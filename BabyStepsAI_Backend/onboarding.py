"""Local-first onboarding, protected voluntary disclosures, and private documents."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from job_search_agent import USER
from secure_data import EncryptionUnavailable, decrypt_text, encrypt_text
from ui_theme import apply_ui_theme

ROOT = Path(__file__).resolve().parent
DB = ROOT / "babysteps.db"
DOCS = ROOT / "private_documents"
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
DISCLOSURES = {"gender", "race_ethnicity", "veteran_status", "disability_status", "sexual_orientation", "pronouns"}
DOCUMENT_TYPES = {"transcript", "cover_letter", "certificate", "portfolio", "other"}
router = APIRouter()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    db=sqlite3.connect(DB,timeout=30,factory=ClosingConnection)
    db.row_factory=sqlite3.Row
    return db


def now():return datetime.now(timezone.utc).isoformat()


def initialize():
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS onboarding_progress(
            user_id TEXT PRIMARY KEY,steps_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL,completed_at TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS sensitive_disclosures(
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,attribute_key TEXT NOT NULL,
            ciphertext TEXT NOT NULL DEFAULT '',declined INTEGER NOT NULL DEFAULT 0,jurisdiction TEXT NOT NULL DEFAULT '',
            reuse_allowed INTEGER NOT NULL DEFAULT 0,ai_allowed INTEGER NOT NULL DEFAULT 0,
            confirmed_at TEXT,updated_at TEXT NOT NULL,revoked_at TEXT,
            UNIQUE(user_id,attribute_key))""")
        db.execute("""CREATE TABLE IF NOT EXISTS application_documents(
            document_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,resume_id TEXT NOT NULL,document_type TEXT NOT NULL,
            original_name TEXT NOT NULL,stored_name TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            FOREIGN KEY(resume_id) REFERENCES resumes(id))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_documents_user_resume ON application_documents(user_id,resume_id,document_type)")


initialize()


def _local_only(request: Request):
    host=getattr(getattr(request,"client",None),"host",None)
    try:
        import ipaddress
        if not host or not ipaddress.ip_address(host).is_loopback:
            raise HTTPException(403,"This local profile endpoint is available only from this device.")
    except ValueError:
        raise HTTPException(403,"This local profile endpoint is available only from this device.")


def _active_resume(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resumes'").fetchone():
        raise HTTPException(409,"Upload a resume before completing onboarding.")
    row=db.execute("SELECT id,stored_name,original_name FROM resumes WHERE user_id=? AND active=1 ORDER BY created_at DESC LIMIT 1",(USER,)).fetchone()
    if not row:raise HTTPException(409,"Upload and activate a resume before completing onboarding.")
    return row


class OnboardingStep(BaseModel):
    completed: bool = True


class SensitiveDisclosureUpdate(BaseModel):
    value: str = Field(default="",max_length=500)
    declined: bool = False
    jurisdiction: str = Field(default="",max_length=120)
    reuse_allowed: bool = False
    ai_allowed: bool = False
    confirmed: bool = False


@router.get("/api/onboarding/state")
def onboarding_state(request:Request):
    _local_only(request)
    with connect() as db:
        try:resume=_active_resume(db)
        except HTTPException:resume=None
        row=db.execute("SELECT steps_json,updated_at,completed_at FROM onboarding_progress WHERE user_id=?",(USER,)).fetchone()
    try:steps=json.loads(row["steps_json"] or "{}") if row else {}
    except (TypeError,json.JSONDecodeError):steps={}
    return {"has_active_resume":bool(resume),"resume_name":resume["original_name"] if resume else None,
            "steps":steps,"updated_at":row["updated_at"] if row else None,
            "completed_at":row["completed_at"] if row else None,"live_submission_enabled":False}


@router.put("/api/onboarding/steps/{step_key}")
def update_onboarding_step(step_key:str,payload:OnboardingStep,request:Request):
    _local_only(request)
    if not re.fullmatch(r"(?:resume|contact|links|experience|documents|authorization|availability|disclosures|answers|permissions|review)",step_key):
        raise HTTPException(404,"Onboarding step not found.")
    stamp=now()
    with connect() as db:
        row=db.execute("SELECT steps_json FROM onboarding_progress WHERE user_id=?",(USER,)).fetchone()
        try:steps=json.loads(row["steps_json"] or "{}") if row else {}
        except (TypeError,json.JSONDecodeError):steps={}
        steps[step_key]=bool(payload.completed)
        completed=stamp if step_key=="review" and payload.completed else None
        db.execute("""INSERT INTO onboarding_progress(user_id,steps_json,updated_at,completed_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET steps_json=excluded.steps_json,updated_at=excluded.updated_at,
            completed_at=COALESCE(excluded.completed_at,onboarding_progress.completed_at)""",
                   (USER,json.dumps(steps,sort_keys=True),stamp,completed))
    return {"saved":True,"steps":steps,"completed_at":completed}


@router.get("/api/sensitive-disclosures")
def get_sensitive_disclosures(request:Request):
    _local_only(request)
    with connect() as db:
        rows=db.execute("SELECT attribute_key,ciphertext,declined,jurisdiction,reuse_allowed,ai_allowed,confirmed_at,updated_at FROM sensitive_disclosures WHERE user_id=? AND revoked_at IS NULL ORDER BY attribute_key",(USER,)).fetchall()
    result=[]
    for row in rows:
        try:value=decrypt_text(row["ciphertext"]) if row["ciphertext"] else ""
        except EncryptionUnavailable as error:raise HTTPException(503,str(error)) from error
        result.append({"key":row["attribute_key"],"value":value,"declined":bool(row["declined"]),
            "jurisdiction":row["jurisdiction"],"reuse_allowed":bool(row["reuse_allowed"]),
            "ai_allowed":bool(row["ai_allowed"]),"confirmed_at":row["confirmed_at"],"updated_at":row["updated_at"]})
    return {"disclosures":result,"ai_use":"not used by matching or answer drafting"}


@router.put("/api/sensitive-disclosures/{attribute_key}")
def save_sensitive_disclosure(attribute_key:str,payload:SensitiveDisclosureUpdate,request:Request):
    _local_only(request)
    if attribute_key not in DISCLOSURES:raise HTTPException(404,"Unknown voluntary disclosure.")
    if payload.declined and payload.value.strip():raise HTTPException(400,"A declined disclosure cannot also contain an answer.")
    if payload.value.strip() and not payload.confirmed:raise HTTPException(400,"Confirm the answer before saving it.")
    if payload.ai_allowed and not payload.reuse_allowed:raise HTTPException(400,"AI use requires explicit reuse permission first.")
    stamp=now()
    try:cipher=encrypt_text(payload.value.strip()) if payload.value.strip() else ""
    except EncryptionUnavailable as error:raise HTTPException(503,str(error)) from error
    with connect() as db:
        db.execute("""INSERT INTO sensitive_disclosures(user_id,attribute_key,ciphertext,declined,jurisdiction,reuse_allowed,ai_allowed,confirmed_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(user_id,attribute_key) DO UPDATE SET ciphertext=excluded.ciphertext,
            declined=excluded.declined,jurisdiction=excluded.jurisdiction,reuse_allowed=excluded.reuse_allowed,
            ai_allowed=excluded.ai_allowed,confirmed_at=excluded.confirmed_at,updated_at=excluded.updated_at,revoked_at=NULL""",
            (USER,attribute_key,cipher,int(payload.declined),payload.jurisdiction.strip(),int(payload.reuse_allowed),
             int(payload.ai_allowed),stamp if payload.confirmed or payload.declined else None,stamp))
    return {"saved":True,"confirmed":bool(payload.confirmed or payload.declined),"ai_allowed":payload.ai_allowed}


@router.delete("/api/sensitive-disclosures/{attribute_key}")
def delete_sensitive_disclosure(attribute_key:str,request:Request):
    _local_only(request)
    if attribute_key not in DISCLOSURES:raise HTTPException(404,"Unknown voluntary disclosure.")
    with connect() as db:db.execute("DELETE FROM sensitive_disclosures WHERE user_id=? AND attribute_key=?",(USER,attribute_key))
    return {"deleted":True,"revoked":True}


def _validate_document(data:bytes,filename:str):
    suffix=Path(filename).suffix.casefold()
    if suffix==".pdf" and data.startswith(b"%PDF-"):return "application/pdf"
    if suffix==".docx":
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names=set(archive.namelist())
                if len(names)>1500 or sum(item.file_size for item in archive.infolist())>60*1024*1024:
                    raise ValueError("The Word document is too large after decompression.")
                if "[Content_Types].xml" in names and "word/document.xml" in names:return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        except (zipfile.BadZipFile,RuntimeError):pass
    if suffix in {".png",".jpg",".jpeg"}:
        if suffix==".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):return "image/png"
        if suffix in {".jpg",".jpeg"} and data.startswith(b"\xff\xd8\xff"):return "image/jpeg"
    raise HTTPException(400,"Choose a valid PDF, DOCX, PNG, or JPEG document.")


@router.post("/api/application-documents",status_code=201)
async def upload_application_document(request:Request,document_type:str=Form(...),file:UploadFile=File(...)):
    _local_only(request)
    if document_type not in DOCUMENT_TYPES:raise HTTPException(400,"Choose a supported document type.")
    filename=Path(file.filename or "document").name.replace("\x00","")[:180]
    if filename in {"",".",".."}:raise HTTPException(400,"The uploaded filename is invalid.")
    data=await file.read(MAX_DOCUMENT_BYTES+1)
    if not data:raise HTTPException(400,"The document is empty.")
    if len(data)>MAX_DOCUMENT_BYTES:raise HTTPException(413,"Documents must be 15 MB or smaller.")
    mime=_validate_document(data,filename)
    document_id=str(uuid.uuid4());stored_name=document_id+Path(filename).suffix.casefold()
    DOCS.mkdir(mode=0o700,parents=True,exist_ok=True)
    if os.name!="nt":os.chmod(DOCS,0o700)
    target=(DOCS/stored_name).resolve()
    if target.parent!=DOCS.resolve():raise HTTPException(400,"The uploaded file path is invalid.")
    target.write_bytes(data)
    stamp=now()
    try:
        with connect() as db:
            resume=_active_resume(db)
            db.execute("""INSERT INTO application_documents(document_id,user_id,resume_id,document_type,original_name,stored_name,mime_type,size_bytes,sha256,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(document_id,USER,resume["id"],document_type,filename,stored_name,mime,len(data),hashlib.sha256(data).hexdigest(),stamp,stamp))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"document_id":document_id,"document_type":document_type,"filename":filename,"size_bytes":len(data),"uploaded_at":stamp}


@router.get("/api/application-documents")
def list_application_documents(request:Request):
    _local_only(request)
    with connect() as db:
        resume=_active_resume(db)
        rows=db.execute("SELECT document_id,document_type,original_name,mime_type,size_bytes,sha256,created_at,updated_at FROM application_documents WHERE user_id=? AND resume_id=? ORDER BY created_at DESC",(USER,resume["id"])).fetchall()
    return {"documents":[dict(row) for row in rows]}


def document_path(document_id:str):
    with connect() as db:
        row=db.execute("SELECT * FROM application_documents WHERE document_id=? AND user_id=?",(document_id,USER)).fetchone()
    if not row:raise HTTPException(404,"Document not found.")
    filename=str(row["stored_name"] or "")
    if Path(filename).name!=filename:raise HTTPException(404,"Document not found.")
    path=(DOCS/filename).resolve()
    if path.parent!=DOCS.resolve() or not path.is_file():raise HTTPException(404,"Document file is missing.")
    return row,path


@router.get("/api/application-documents/{document_id}/download")
def download_application_document(document_id:str,request:Request):
    _local_only(request)
    row,path=document_path(document_id)
    return FileResponse(path,media_type=row["mime_type"],filename=row["original_name"],headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})


@router.delete("/api/application-documents/{document_id}")
def delete_application_document(document_id:str,request:Request):
    _local_only(request)
    try:row,path=document_path(document_id)
    except HTTPException as error:
        if error.status_code==404:return {"deleted":True}
        raise
    with connect() as db:db.execute("DELETE FROM application_documents WHERE document_id=? AND user_id=?",(document_id,USER))
    path.unlink(missing_ok=True)
    return {"deleted":True}


ONBOARDING_PAGE=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>Welcome — BabyStepsAI</title>
<style>
.notice.success{color:#137451}.notice.error{color:#a3261d}.field input[aria-invalid="true"],.field textarea[aria-invalid="true"],.field select[aria-invalid="true"]{border:2px solid #b42318;box-shadow:0 0 0 3px #b4231822}
:root{--ink:#17233c;--muted:#697792;--line:#dce3ef;--wash:#f4f6fb;--surface:#fff;--accent:#6757ed;--mint:#c9f57b;--cyan:#83e6dd;--orange:#ffae73;--soft:#f0efff;--shadow:0 24px 70px #23304f14}*{box-sizing:border-box}body{margin:0;background:radial-gradient(800px 480px at 0% 0%,#d8d7ff88,transparent 68%),radial-gradient(700px 420px at 100% 0%,#c9f57b44,transparent 70%),var(--wash);color:var(--ink);font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:1240px;margin:28px auto 70px;padding:0 24px}.mast{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}.wordmark{font-weight:900;letter-spacing:-.06em;font-size:20px}.wordmark i{font-style:normal;color:var(--accent)}.save-state{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:#9edb58;box-shadow:0 0 0 4px #9edb5825}.hero{display:grid;grid-template-columns:1fr auto;align-items:end;gap:20px;padding:31px 34px;border-radius:28px;background:#192440;color:#fff;position:relative;overflow:hidden;box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;width:320px;height:320px;right:8%;top:-220px;border:1px solid #ffffff25;border-radius:50%;box-shadow:0 0 0 35px #ffffff09,0 0 0 70px #ffffff06}.eyebrow{text-transform:uppercase;font-weight:800;letter-spacing:.13em;font-size:11px;color:var(--mint)}h1{font-size:clamp(30px,5vw,52px);letter-spacing:-.06em;line-height:1.03;margin:7px 0 12px;max-width:700px}.hero p{color:#c2cbe0;margin:0;max-width:640px}.hero-stat{position:relative;z-index:1;background:#ffffff12;border:1px solid #ffffff21;border-radius:18px;padding:15px 18px;min-width:155px}.hero-stat strong{display:block;font-size:26px;line-height:1.1}.hero-stat span{font-size:12px;color:#c2cbe0}.layout{display:grid;grid-template-columns:270px minmax(0,1fr);gap:20px;margin-top:20px}.rail,.workspace{border:1px solid var(--line);background:color-mix(in srgb,var(--surface) 94%,transparent);border-radius:23px;box-shadow:var(--shadow)}.rail{padding:15px;height:max-content;position:sticky;top:18px}.rail-title{font-size:11px;font-weight:850;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);padding:8px 10px 12px}.step{display:grid;grid-template-columns:34px 1fr auto;gap:10px;align-items:center;border:0;background:transparent;text-align:left;width:100%;padding:10px;border-radius:13px;color:var(--ink);cursor:pointer;font:inherit}.step:hover{background:var(--wash)}.step.active{background:var(--soft);color:#4f43cd}.step.complete .num{background:var(--mint);color:#243218}.num{width:31px;height:31px;border-radius:10px;display:grid;place-items:center;background:var(--wash);font-size:12px;font-weight:850}.step-label{font-size:13px;font-weight:750}.step-hint{display:block;color:var(--muted);font-size:10px;font-weight:500}.check{color:#628d26}.rail-bottom{padding:14px 10px 4px;border-top:1px solid var(--line);margin-top:10px;color:var(--muted);font-size:12px}.workspace{min-height:590px;padding:clamp(20px,4vw,40px);position:relative;overflow:hidden}.progressline{height:6px;border-radius:9px;background:var(--wash);margin-bottom:28px;overflow:hidden}.progressline i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#8bdb70);border-radius:9px;transition:width .3s}.section-kicker{font-size:11px;color:var(--accent);font-weight:900;letter-spacing:.13em;text-transform:uppercase}.workspace h2{font-size:clamp(25px,4vw,36px);letter-spacing:-.05em;margin:5px 0 7px}.sub{color:var(--muted);max-width:670px;margin:0 0 24px}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.field{display:flex;flex-direction:column;gap:6px;margin:4px 0 12px}.field.full{grid-column:1/-1}.field label{font-size:12px;font-weight:800}.field input,.field textarea,.field select{width:100%;font:inherit;color:var(--ink);padding:12px 13px;border:1px solid var(--line);border-radius:12px;background:var(--surface);outline:none;transition:border-color .15s,box-shadow .15s}.field input:focus,.field textarea:focus,.field select:focus{border-color:var(--accent);box-shadow:0 0 0 4px #6757ed20}.field textarea{min-height:104px;resize:vertical}.confirm{display:flex;align-items:flex-start;gap:8px;color:var(--muted);font-size:11px;margin-top:1px}.confirm input{accent-color:var(--accent);margin:2px 0 0;width:auto}.help{font-size:11px;color:var(--muted)}.optional{display:inline-block;border-radius:30px;background:var(--wash);padding:2px 7px;color:var(--muted);font-size:10px;margin-left:5px}.actions{display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid var(--line);margin-top:28px;padding-top:18px}.action-right{display:flex;gap:9px;flex-wrap:wrap}.button{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:12px;padding:11px 16px;font:750 13px inherit;cursor:pointer;transition:transform .12s,box-shadow .12s}.button:hover{transform:translateY(-1px);box-shadow:0 8px 20px #1924401a}.button.primary{background:var(--accent);border-color:var(--accent);color:white}.button.ghost{border-color:transparent;background:transparent;color:var(--muted)}.button:disabled{opacity:.5;cursor:not-allowed;transform:none}.callout{padding:13px 15px;border-radius:14px;background:var(--wash);color:var(--muted);font-size:12px;margin:14px 0}.callout.safe{background:#eff8e5;color:#466226}.callout.warn{background:#fff2e8;color:#87552d}.repeat{border:1px solid var(--line);border-radius:16px;padding:14px;margin:10px 0;background:var(--wash)}.repeat-head{display:flex;justify-content:space-between;align-items:center;font-size:13px;font-weight:800;margin-bottom:8px}.repeater{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 12px}.empty{border:1px dashed var(--line);border-radius:15px;padding:17px;color:var(--muted);font-size:13px}.choice-row{display:flex;flex-wrap:wrap;gap:8px}.choice{border:1px solid var(--line);border-radius:999px;padding:8px 12px;cursor:pointer}.choice:has(input:checked){background:var(--soft);border-color:var(--accent)}.choice input{accent-color:var(--accent)}.doc-row,.answer-row,.review-row{display:grid;grid-template-columns:1fr auto;align-items:center;gap:12px;border:1px solid var(--line);padding:13px;border-radius:14px;margin:8px 0}.doc-row small,.review-row small{display:block;color:var(--muted)}.notice{color:#a34343;min-height:24px;font-size:13px}.spark{color:var(--mint);font-size:18px}footer{margin-top:18px;color:var(--muted);font-size:11px;display:flex;justify-content:space-between;gap:12px}.hidden{display:none!important}
@media(max-width:850px){.shell{padding:0 14px;margin-top:15px}.layout{grid-template-columns:1fr}.rail{position:static;overflow:auto}.step-list{display:flex;min-width:max-content}.rail-title,.rail-bottom{display:none}.step{width:170px}.hero{padding:25px 22px}.hero-stat{display:none}.workspace{min-height:520px}}@media(max-width:560px){.mast{margin-bottom:14px}.hero{border-radius:20px;padding:22px 18px}.layout{gap:12px;margin-top:12px}.workspace{border-radius:18px;padding:20px 16px}.field-grid,.repeater{grid-template-columns:1fr}.field.full{grid-column:auto}.actions{align-items:stretch;flex-direction:column-reverse}.actions>.button,.action-right{width:100%}.action-right .button{flex:1}.step{width:145px}.step-hint{display:none}}
</style></head><body><main class="shell"><header class="mast"><div class="wordmark">BabySteps<i>AI</i><span class="spark">✳</span></div><div class="save-state"><i class="dot"></i><span id="save-state">Your progress saves as you go</span></div></header><section class="hero"><div><div class="eyebrow">Your profile, thoughtfully prepared</div><h1>Let’s make the next application feel easier.</h1><p>Build a private information bank once. You stay in control of what is confirmed, reused, or shared.</p></div><div class="hero-stat"><strong id="hero-count">0 / 11</strong><span>chapters complete</span></div></section><div class="layout"><aside class="rail"><div class="rail-title">Your setup</div><div id="step-list" class="step-list"></div><div class="rail-bottom">Optional questions stay optional.<br>Live submissions are off.</div></aside><section class="workspace" aria-live="polite"><div class="progressline"><i id="progress"></i></div><div id="step-content"></div><p id="notice" class="notice" role="status"></p><div class="actions"><button class="button ghost" id="back" type="button">← Back</button><div class="action-right"><button class="button ghost" id="skip" type="button">Skip optional</button><button class="button primary" id="continue" type="button">Save & continue →</button></div></div></section></div><footer><span>Private by design · You can edit these details later</span><a href="/candidate-profile">Open full profile ↗</a></footer></main>
<script>
const steps=[['resume','Resume & verification','Start here'],['contact','You','Personal details'],['links','Your links','Professional presence'],['experience','Your story','Education & experience'],['documents','Documents','Private files'],['authorization','Work eligibility','Country by country'],['availability','What works for you','Availability'],['disclosures','Your choice','Optional disclosures'],['answers','Answer library','Reusable answers'],['permissions','Your boundaries','Automation'],['review','Ready when you are','Final review']];
const fieldLabels={full_name:'Full name',email:'Email address',phone:'Phone number',country:'Country',city:'City',state_province:'State / province',postal_code:'Postal code',address_line1:'Address line 1',address_line2:'Address line 2',phone_country_code:'Phone country code',linkedin:'LinkedIn URL',github:'GitHub URL',portfolio:'Portfolio URL',website:'Personal website',other_professional_links:'Other professional URLs',university:'School or university',degree:'Degree',major:'Major / field of study',graduation_date:'Graduation month',earliest_available_date:'Earliest start date',notice_period:'Notice period',full_time_availability:'Full-time availability',internship_availability:'Internship availability',availability:'Availability notes',willingness_to_travel:'Willingness to travel',relocation_willingness:'Willingness to relocate',work_arrangement_preferences:'Remote, hybrid, or on-site',preferred_locations:'Preferred cities or regions',preferred_countries:'Preferred countries',salary_expectations:'Salary expectations (optional)',salary_currency:'Currency'};
const stepsForm={contact:['full_name','email','phone','country','city','state_province','postal_code','address_line1','address_line2','phone_country_code'],links:['linkedin','github','portfolio','website','other_professional_links'],availability:['earliest_available_date','notice_period','full_time_availability','internship_availability','willingness_to_travel','relocation_willingness','work_arrangement_preferences','preferred_locations','preferred_countries','salary_expectations','salary_currency']};
const disclosureLabels={gender:'Gender',race_ethnicity:'Race and ethnicity',veteran_status:'Veteran status',disability_status:'Disability status',sexual_orientation:'Sexual orientation',pronouns:'Pronouns'};
let index=0,state={steps:{}},profile=null,answers=[],documents=[],disclosures=[],authorization={};const $=s=>document.querySelector(s);
async function api(url,options={}){const response=await fetch(url,{...options,headers:{...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...(options.headers||{})}});let data={};try{data=await response.json()}catch{}if(!response.ok){const detail=data.detail;const message=Array.isArray(detail)?detail.map(x=>(Array.isArray(x.loc)?x.loc.filter(y=>y!=='body').join(' → ')+': ':'')+(x.msg||'Invalid value')).join('; '):typeof detail==='string'?detail:'Could not save this step. Check the highlighted fields and try again.';const error=Error(message);error.detail=detail;throw error}return data}
function button(text,action,kind='button'){const b=document.createElement('button');b.className='button '+kind;b.type='button';b.textContent=text;b.onclick=action;return b}
function element(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node}
function field(key,value,options={}){const wrap=element('div','field'+(options.full?' full':'')),label=element('label','',fieldLabels[key]||key.replaceAll('_',' ')),required=['full_name','email'].includes(key);label.append(element('span','optional',required?'Required for basic preparation':'Optional'));wrap.append(label);let input;if(options.kind==='textarea'||options.kind==='list'){input=document.createElement('textarea');input.value=Array.isArray(value)?value.join('\n'):String(value||'');if(options.kind==='list')input.placeholder='One per line'}else{input=document.createElement('input');input.type=options.kind||(['earliest_available_date'].includes(key)?'date':['email','phone'].includes(key)?key==='phone'?'tel':'email':key.includes('date')?'month':key.match(/linkedin|github|portfolio|website|links/)?'url':'text');input.value=String(value||'');if(input.type==='month'&&/^\d{4}-\d{2}-\d{2}$/.test(input.value))input.value=input.value.slice(0,7);if(input.type==='date'&&/^\d{4}-\d{2}$/.test(input.value))input.value+='-01'}input.id='onboarding-'+key.replaceAll(':','-');label.htmlFor=input.id;input.dataset.key=key;wrap.append(input);if(options.confirm){const check=element('label','confirm');const cb=document.createElement('input');cb.type='checkbox';cb.checked=!!options.confirmed;cb.dataset.confirm=key;check.append(cb,document.createTextNode(' I reviewed this and confirm it is accurate'));wrap.append(check)}return wrap}
function renderRail(){const list=$('#step-list');list.replaceChildren();steps.forEach(([key,title,hint],i)=>{const b=element('button','step'+(i===index?' active':'')+(state.steps[key]?' complete':''));b.type='button';const n=element('span','num',state.steps[key]?'✓':String(i+1));const text=element('span','step-label',title);text.append(element('span','step-hint',hint));b.append(n,text);if(state.steps[key])b.append(element('span','check','✓'));b.onclick=()=>{index=i;render()};list.append(b)});const complete=Object.values(state.steps).filter(Boolean).length;$('#hero-count').textContent=complete+' / '+steps.length;$('#progress').style.width=(100*complete/steps.length)+'%'}
function renderFields(keys,full=false){const grid=element('div','field-grid');keys.forEach(k=>{const f=profile?.fields?.find(x=>x.key===k)||{value:'',confirmed:false};grid.append(field(k,f.value,{kind:k==='other_professional_links'||k==='preferred_locations'||k==='preferred_countries'||k==='work_arrangement_preferences'?'list':undefined,full:['address_line1','address_line2','other_professional_links','preferred_locations','preferred_countries','work_arrangement_preferences'].includes(k),optional:!['full_name','email'].includes(k),confirm:true,confirmed:f.confirmed}))});return grid}
function title(kicker,title,sub){const box=document.createDocumentFragment();box.append(element('div','section-kicker',kicker),element('h2','',title),element('p','sub',sub));return box}
function render(message=''){renderRail();const key=steps[index][0],content=$('#step-content');content.replaceChildren();$('#notice').textContent=message;$('#notice').className='notice'+(message?' success':'');$('#back').disabled=index===0;$('#skip').classList.toggle('hidden',['resume','contact','review'].includes(key));$('#continue').textContent=key==='review'?'Finish setup ✓':'Save & continue →';content.append(title('Chapter '+String(index+1).padStart(2,'0'),steps[index][1],steps[index][2]));({resume:renderResume,contact:renderContact,links:renderLinks,experience:renderExperience,documents:renderDocuments,authorization:renderAuthorization,availability:renderAvailability,disclosures:renderDisclosures,answers:renderAnswers,permissions:renderPermissions,review:renderReview}[key]||(()=>{}))(content)}
function renderResume(root){const card=element('div','callout safe','Your active resume powers matching and form preparation. Resume analysis creates suggestions only; your confirmation makes them usable facts.');root.append(card,element('div','doc-row',state.has_active_resume?('✓  '+(state.resume_name||'Active resume')):'No active resume yet'));const form=document.createElement('form');form.id='resume-upload';form.innerHTML='<label class="field"><span>Upload a text-based PDF resume · up to 5 MB</span><input name="file" type="file" accept="application/pdf,.pdf" required></label><button class="button" type="submit">Upload and activate resume</button>';form.onsubmit=async e=>{e.preventDefault();try{const d=await api('/resumes',{method:'POST',body:new FormData(form)});state=await api('/api/onboarding/state');profile=await api('/api/candidate-profile/verification');if(!state.has_active_resume||profile.resume_name!==d.filename)throw Error('The uploaded resume could not be verified as active.');answers=await api('/api/application-answers');documents=await api('/api/application-documents');disclosures=(await api('/api/sensitive-disclosures')).disclosures;const completion=await api('/api/candidate-profile/completeness');render();showSaved(completion);$('#notice').prepend(document.createTextNode('Resume uploaded and verified. '))}catch(err){$('#notice').textContent=err.message}};root.append(form,element('a','','Review your resume versions →'));root.lastChild.href='/';}
function renderContact(root){root.append(renderFields(stepsForm.contact));root.append(element('div','callout','Every field has its own confirmation. Information from a resume remains a suggestion until you confirm it.'))}
function renderLinks(root){root.append(renderFields(stepsForm.links));root.append(element('div','callout safe','Only HTTPS professional links are accepted. Remove a link by clearing it, then save.'))}
function renderExperience(root){for(const k of ['education_entries','experience_entries','internship_entries','project_entries']){const data=profile.fields.find(x=>x.key===k)||{value:[],entry_confirmed:[]};const wrapper=element('section','repeat'),head=element('div','repeat-head',({education_entries:'Education',experience_entries:'Professional experience',internship_entries:'Internships',project_entries:'Projects'})[k]);head.append(button('+ Add',()=>{data.value.push(k==='education_entries'?{institution:'',degree:'',major:'',graduation_date:'',gpa:'',currently_enrolled:false}:k==='project_entries'?{name:'',description:'',technologies:[],github_url:'',demo_url:'',accomplishments:[]}:{job_title:'',employer:'',location:'',employment_type:'',start_date:'',end_date:'',currently_working:false,responsibilities:[],technologies:[]});data.entry_confirmed.push(false);render()}));wrapper.append(head);if(!data.value.length)wrapper.append(element('div','empty','Nothing added yet. Add details you want available for applications.'));data.value.forEach((item,i)=>{const card=element('div','repeat');const row=element('div','repeater');const allowed=Object.keys(item);allowed.forEach(prop=>{if(['currently_enrolled','currently_working'].includes(prop)){const label=element('label','confirm'),cb=document.createElement('input');cb.type='checkbox';cb.checked=!!item[prop];cb.onchange=()=>item[prop]=cb.checked;label.append(cb,document.createTextNode(' '+prop.replaceAll('_',' ')));row.append(label);return}const f=field(k+'_'+i+'_'+prop,item[prop],{kind:Array.isArray(item[prop])?'list':prop.includes('date')?'month':prop==='description'||prop==='responsibilities'||prop==='accomplishments'?'textarea':undefined,full:['description','responsibilities','accomplishments','technologies'].includes(prop)});const inp=f.querySelector('input,textarea');inp.oninput=()=>item[prop]=['responsibilities','accomplishments','technologies'].includes(prop)?inp.value.split('\n').filter(Boolean):inp.value;row.append(f)});card.append(row);const confirm=element('label','confirm'),cb=document.createElement('input');cb.type='checkbox';cb.checked=!!data.entry_confirmed[i];cb.onchange=()=>data.entry_confirmed[i]=cb.checked;confirm.append(cb,document.createTextNode(' I confirm this entry'));card.append(confirm,button('Remove',()=>{data.value.splice(i,1);data.entry_confirmed.splice(i,1);render()},'ghost'));wrapper.append(card)});root.append(wrapper)}root.append(element('div','callout','Do not add achievements, grades, certificates, or employment details that are not yours.'))}
function renderDocuments(root){const form=document.createElement('form');form.id='doc-form';form.innerHTML='<div class="field-grid"><label class="field"><span>Document category</span><select name="document_type"><option value="transcript">Academic transcript</option><option value="cover_letter">Cover letter</option><option value="certificate">Certificate</option><option value="portfolio">Portfolio document</option><option value="other">Other requested attachment</option></select></label><label class="field"><span>File · PDF, DOCX, PNG, JPEG · max 15 MB</span><input name="file" type="file" accept=".pdf,.docx,.png,.jpg,.jpeg" required></label></div><button class="button primary">Upload privately</button>';form.onsubmit=async e=>{e.preventDefault();try{const uploaded=await api('/api/application-documents',{method:'POST',body:new FormData(form)});documents=(await api('/api/application-documents')).documents;if(!documents.some(x=>x.document_id===uploaded.document_id))throw Error('The uploaded document could not be verified.');const completion=await api('/api/candidate-profile/completeness');render();showSaved(completion);$('#notice').prepend(document.createTextNode('Private document saved and verified. '))}catch(err){$('#notice').className='notice error';$('#notice').textContent='Save failed: '+err.message}};root.append(form);root.append(element('p','sub','Files are stored in a private folder and are never served from the public site.'));if(!documents.length)root.append(element('div','empty','Your private document shelf is empty. The active resume remains managed separately.'));documents.forEach(doc=>{const row=element('div','doc-row'),info=element('div','',doc.original_name);info.append(element('small','',doc.document_type.replaceAll('_',' ')+' · '+Math.ceil(doc.size_bytes/1024)+' KB'));row.append(info,button('Remove',async()=>{await api('/api/application-documents/'+doc.document_id,{method:'DELETE'});documents=(await api('/api/application-documents')).documents;render()},'ghost'));root.append(row)})}
function renderAuthorization(root){root.append(element('div','callout warn','Immigration status is never inferred from a resume, name, location, or photo. Unknown is a valid answer. These answers are encrypted on this device.'));const currentField=profile.fields.find(x=>x.key==='country_work_authorizations'),current=currentField?.value||[];current.forEach((item,i)=>{const card=element('div','repeat'),grid=element('div','repeater'),country=field('auth-country-'+i,item.country||'',{full:true});country.querySelector('label').textContent='Country';country.querySelector('input').oninput=e=>item.country=e.target.value;const status=element('label','field');status.append(element('span','','Current work authorization'));const select=document.createElement('select');select.innerHTML='<option>Unknown</option><option>Authorized</option><option>Not authorized</option><option>Other confirmed restriction</option>';select.value=item.status||'Unknown';select.onchange=()=>item.status=select.value;status.append(select);grid.append(country,status);card.append(grid);const confirm=element('label','confirm'),cb=document.createElement('input');cb.type='checkbox';cb.checked=!!currentField.entry_confirmed?.[i];cb.dataset.entryConfirm='country_work_authorizations:'+i;confirm.append(cb,document.createTextNode(' I explicitly confirm this answer'));card.append(confirm,button('Remove country',()=>{current.splice(i,1);currentField.entry_confirmed?.splice(i,1);render()},'ghost'));root.append(card)});root.append(button('+ Add country',()=>{current.push({country:'',status:'Unknown'});currentField.value=current;currentField.entry_confirmed=currentField.entry_confirmed||[];currentField.entry_confirmed.push(false);render()}));const sponsorField=profile.fields.find(x=>x.key==='sponsorship_by_country'),sponsorship=sponsorField?.value||[];sponsorship.forEach((item,i)=>{const card=element('div','repeat'),grid=element('div','repeater'),country=field('sponsor-country-'+i,item.country||'',{full:true});country.querySelector('label').textContent='Country';country.querySelector('input').oninput=e=>item.country=e.target.value;const f=element('label','field');f.append(element('span','','Sponsorship required now or later'));const select=document.createElement('select');select.innerHTML='<option>Unknown</option><option>No</option><option>Yes, now</option><option>Yes, in the future</option>';select.value=item.requires_sponsorship||'Unknown';select.onchange=()=>item.requires_sponsorship=select.value;f.append(select);grid.append(country,f);card.append(grid);const confirm=element('label','confirm'),cb=document.createElement('input');cb.type='checkbox';cb.checked=!!sponsorField.entry_confirmed?.[i];cb.dataset.entryConfirm='sponsorship_by_country:'+i;confirm.append(cb,document.createTextNode(' I explicitly confirm this answer'));card.append(confirm,button('Remove',()=>{sponsorship.splice(i,1);sponsorField.entry_confirmed?.splice(i,1);render()},'ghost'));root.append(card)});root.append(button('+ Add sponsorship answer',()=>{sponsorship.push({country:'',requires_sponsorship:'Unknown'});sponsorField.value=sponsorship;sponsorField.entry_confirmed=sponsorField.entry_confirmed||[];sponsorField.entry_confirmed.push(false);render()}),element('div','callout','You can leave a country as unknown. Specific restrictions are optional; confirm each answer before saving.'))}
function renderAvailability(root){root.append(renderFields(stepsForm.availability));root.append(element('div','callout','Dates and preferences are used only when you choose to prepare a dry run. Nothing is inferred from past applications.'))}
function renderDisclosures(root){root.append(element('div','callout','Every disclosure is optional. You can decline, revoke, or delete any answer. These fields are encrypted separately and never enter job ranking or matching.'));Object.entries(disclosureLabels).forEach(([key,label])=>{const saved=disclosures.find(x=>x.key===key)||{},card=element('div','repeat'),f=field('disclosure_'+key,saved.value||'',{optional:true});f.querySelector('label').textContent=label;const select=document.createElement('select');select.innerHTML='<option value="">Choose if you want</option><option>Prefer not to say</option><option>Self describe</option>';f.append(select);const controls=element('div','choice-row');const decline=document.createElement('label');decline.className='choice';decline.innerHTML='<input type="checkbox" '+(saved.declined?'checked':'')+'> Prefer not to answer';const reuse=document.createElement('label');reuse.className='choice';reuse.innerHTML='<input type="checkbox" '+(saved.reuse_allowed?'checked':'')+'> Allow accurate reuse in an application';card.append(f,decline,reuse,button('Save choice',async()=>{const input=f.querySelector('input'),d=decline.querySelector('input'),r=reuse.querySelector('input');try{const submitted={value:d.checked?'':input.value,declined:d.checked,confirmed:d.checked||!!input.value,reuse_allowed:r.checked,ai_allowed:false};const saved=await api('/api/sensitive-disclosures/'+key,{method:'PUT',body:JSON.stringify(submitted)});if(!saved.saved)throw Error('The disclosure choice was not confirmed by the server.');disclosures=(await api('/api/sensitive-disclosures')).disclosures;const persisted=disclosures.find(x=>x.key===key);if(!persisted||persisted.value!==submitted.value||persisted.declined!==submitted.declined||persisted.reuse_allowed!==submitted.reuse_allowed||persisted.ai_allowed!==false||(submitted.confirmed&&!persisted.confirmed))throw Error('The disclosure choice could not be verified.');$('#notice').className='notice success';$('#notice').textContent='Optional disclosure choice saved and verified securely.'}catch(err){$('#notice').textContent=err.message}}),button('Delete',async()=>{await api('/api/sensitive-disclosures/'+key,{method:'DELETE'});disclosures=(await api('/api/sensitive-disclosures')).disclosures;render()},'ghost'));root.append(card)})}
function renderAnswers(root){root.append(element('p','callout','Confirmed answers can be tied to a question, source, jurisdiction, and future reuse permission. Sensitive/legal answers are never auto-reused.'));const form=document.createElement('form');form.id='answer-form';form.innerHTML='<label class="field"><span>Question or category</span><input name="question" required maxlength="300" placeholder="What is your earliest available start date?"></label><label class="field"><span>Your confirmed answer</span><textarea name="answer" required maxlength="4000"></textarea></label><label class="field"><span>Jurisdiction (optional)</span><input name="jurisdiction" maxlength="120"></label><label class="confirm"><input type="checkbox" name="reuse"> Allow future reuse of this ordinary factual answer</label><button class="button primary">Confirm and save answer</button>';form.onsubmit=async e=>{e.preventDefault();const data=new FormData(form),key='user-'+crypto.randomUUID().replaceAll('-','').slice(0,20);try{const submitted={question:data.get('question'),answer:data.get('answer'),jurisdiction:data.get('jurisdiction'),confirmed:true,auto_reuse:data.has('reuse'),sensitive:false,answer_type:'factual'};const saved=await api('/api/application-answers/'+key,{method:'PUT',body:JSON.stringify(submitted)});if(!saved.saved)throw Error('The answer was not confirmed by the server.');answers=await api('/api/application-answers');const persisted=answers.find(x=>x.key==='custom:'+key||x.key===key);if(!persisted||persisted.answer!==submitted.answer||persisted.question!==submitted.question||persisted.auto_reuse!==submitted.auto_reuse)throw Error('The saved answer could not be verified.');render();$('#notice').className='notice success';$('#notice').textContent='Confirmed answer saved and verified.'}catch(err){$('#notice').className='notice error';$('#notice').textContent='Save failed: '+err.message}};root.append(form);answers.forEach(item=>{const row=element('div','answer-row'),info=element('div','',item.question);info.append(element('small','',item.sensitive?'Sensitive · requires your input':item.auto_reuse?'Confirmed · approved for reuse':'Confirmed · reuse off'));row.append(info,button('Revoke',async()=>{await api('/api/application-answers/'+item.key.replace(/^custom:/,''),{method:'DELETE'});answers=await api('/api/application-answers');render()},'ghost'));root.append(row)})}
function renderPermissions(root){const settings=authorization.settings||{};root.append(element('div','callout warn','Permissions are separate choices. BabyStepsAI never submits an application. Employer declarations and human verification always pause for you.'));[['allow_automatic_discovery','Allow job discovery'],['allow_ai_matching','Allow AI job matching'],['allow_automated_form_preparation','Allow dry-run form preparation for jobs I select'],['allow_auto_use_confirmed_answers','Allow confirmed facts in supported forms'],['allow_demographic_reuse','Allow explicitly approved demographic reuse when the match is exact'],['allow_ai_drafting','Allow AI drafts for ordinary written questions']].forEach(([key,label])=>{const item=element('label','choice'),cb=document.createElement('input');cb.type='checkbox';cb.checked=!!settings[key];cb.dataset.setting=key;item.append(cb,document.createTextNode(' '+label));root.append(item)});root.append(element('p','callout safe','Live submission is permanently disabled in this phase.'))}
function renderReview(root){const rows=[['Resume',state.resume_name||'Not uploaded'],['Confirmed profile facts',profile.fields.filter(x=>x.confirmed).length],['Reusable answers',answers.length],['Private documents',documents.length],['Optional disclosures',disclosures.length],['Live submission','Disabled']];rows.forEach(([label,value])=>{const item=element('div','review-row'),text=element('div','',label);text.append(element('small','',String(value)));item.append(text,element('span','spark','✳'));root.append(item)});root.append(element('div','callout safe','You can edit these choices later from Candidate Profile, Application Settings, and this onboarding page. Only confirmed information is available to matching and supported dry runs.'))}
function profilePayload(){const values={},confirmed=[];for(const f of profile.fields){values[f.key]=f.value;if(f.confirmed)confirmed.push(f.key);if(f.kind==='objects')(f.entry_confirmed||[]).forEach((yes,i)=>{if(yes)confirmed.push(f.key+':'+i)})}return {values,confirmed_fields:confirmed}}
function collectValues(keys){const values=profilePayload().values,confirmed=profilePayload().confirmed_fields;keys.forEach(k=>{const f=profile.fields.find(x=>x.key===k);if(!f)return;const el=document.querySelector('[data-key="'+k+'"]');if(!el)return;let value=el.value;if(f.kind==='list'||['other_professional_links','preferred_locations','preferred_countries','work_arrangement_preferences'].includes(k))value=value.split('\n').map(x=>x.trim()).filter(Boolean);values[k]=value;const at=confirmed.indexOf(k);if(document.querySelector('[data-confirm="'+k+'"]')?.checked){if(at<0)confirmed.push(k)}else if(at>=0)confirmed.splice(at,1)});return {values,confirmed_fields:confirmed}}
function stable(value){if(Array.isArray(value))return '['+value.map(stable).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+stable(value[k])).join(',')+'}';return JSON.stringify(value)}
function showSaved(completion){const notice=$('#notice'),items=completion.attention_items||[];notice.replaceChildren();notice.className='notice success';notice.append(document.createTextNode(items.length?'Saved — '+items.length+' items need attention.':'Saved — no required items need attention.'));if(items.length){const list=document.createElement('ul');items.forEach(item=>{const li=document.createElement('li');li.textContent=item.label+': '+item.reason+' ('+item.requiredness+').';list.append(li)});notice.append(list)}if(completion.optional?.length)notice.append(element('p','help',completion.optional.length+' optional fields remain blank or unconfirmed; this does not block saving.'));if(completion.job_specific?.length)notice.append(element('p','help',completion.job_specific.length+' job-specific requirements are listed separately in your profile.'))}
function flagInvalid(message){const low=String(message||'').toLowerCase();const field=[...document.querySelectorAll('[data-key]')].find(x=>low.includes(fieldLabels[x.dataset.key]?.toLowerCase()||'\u0000'));if(field){field.setAttribute('aria-invalid','true');field.focus()}}
async function verifyProfileSave(values,confirmed,keys){const persisted=await api('/api/candidate-profile/verification'),byKey=Object.fromEntries(persisted.fields.map(x=>[x.key,x]));for(const key of keys){const item=byKey[key];if(!item||stable(item.value)!==stable(values[key]))throw Error('Saved '+(fieldLabels[key]||key)+' could not be verified.');if(item.kind==='objects'){for(let i=0;i<(values[key]||[]).length;i++)if(!!item.entry_confirmed?.[i]!==confirmed.includes(key+':'+i))throw Error('Confirmation for '+(fieldLabels[key]||key)+' entry '+(i+1)+' could not be verified.')}else if(!!item.confirmed!==confirmed.includes(key))throw Error('Confirmation for '+(fieldLabels[key]||key)+' could not be verified.')}profile=persisted;return persisted}
async function saveStep(){const key=steps[index][0];if(key==='resume'&&!state.has_active_resume)throw Error('Upload and activate a resume before continuing.');let checkedValues=null,confirmed=[];if(['contact','links','availability'].includes(key)){const result=collectValues(stepsForm[key]);checkedValues=result.values;confirmed=result.confirmed_fields;for(const k of ['linkedin','github','portfolio','website'])if(result.values[k]&&!/^https:\/\//i.test(result.values[k])){const input=document.querySelector('[data-key="'+k+'"]');input?.setAttribute('aria-invalid','true');input?.focus();throw Error(fieldLabels[k]+' must start with https://. Update this field or clear it; all link fields are optional.')}const email=document.querySelector('[data-key="email"]');if(result.values.email&&email&&!email.checkValidity()){email.setAttribute('aria-invalid','true');email.focus();throw Error('Enter a valid email address or clear the optional field.')}const saved=await api('/api/candidate-profile/verification',{method:'PUT',body:JSON.stringify(result)});if(!saved.saved)throw Error('The server did not confirm the profile save.');await verifyProfileSave(checkedValues,confirmed,stepsForm[key])}else if(['experience','authorization'].includes(key)){const data=profilePayload();if(key==='authorization'){data.confirmed_fields=data.confirmed_fields.filter(k=>!/^country_work_authorizations:\d+$|^sponsorship_by_country:\d+$/.test(k));document.querySelectorAll('[data-entry-confirm]').forEach(x=>{if(x.checked)data.confirmed_fields.push(x.dataset.entryConfirm)})}checkedValues=data.values;confirmed=data.confirmed_fields;const saved=await api('/api/candidate-profile/verification',{method:'PUT',body:JSON.stringify(data)});if(!saved.saved)throw Error('The server did not confirm the profile save.');await verifyProfileSave(checkedValues,confirmed,[key==='experience'?'education_entries':'country_work_authorizations',key==='experience'?'experience_entries':'sponsorship_by_country',...(key==='experience'?['internship_entries','project_entries']:[])])}else if(key==='permissions'){const body={...authorization.settings};document.querySelectorAll('[data-setting]').forEach(x=>body[x.dataset.setting]=x.checked);const saved=await api('/api/application-authorization',{method:'PUT',body:JSON.stringify(body)});if(!saved.saved)throw Error('The server did not confirm the permission save.');authorization=await api('/api/application-authorization');for(const [name,value] of Object.entries(body))if(authorization.settings[name]!==value)throw Error('The saved '+name.replaceAll('_',' ')+' permission could not be verified.')}const stepSaved=await api('/api/onboarding/steps/'+key,{method:'PUT',body:JSON.stringify({completed:true})});if(!stepSaved.saved)throw Error('The server did not confirm this onboarding step.');state=await api('/api/onboarding/state');if(state.steps[key]!==true)throw Error('The saved onboarding progress could not be verified.');return await api('/api/candidate-profile/completeness')}
async function load(){try{state=await api('/api/onboarding/state');if(state.has_active_resume){[profile,answers,documents,disclosures,authorization]=await Promise.all([api('/api/candidate-profile/verification'),api('/api/application-answers'),api('/api/application-documents'),api('/api/sensitive-disclosures'),api('/api/application-authorization')])}else{profile={fields:[]};answers=[];documents=[];disclosures=[];authorization={settings:{}}}index=Math.max(0,steps.findIndex(x=>!state.steps[x[0]]));if(index<0)index=steps.length-1;render()}catch(e){$('#notice').textContent=e.message;render()}}
$('#back').onclick=()=>{if(index>0){index--;render()}};$('#continue').onclick=async()=>{const button=$('#continue');button.disabled=true;button.setAttribute('aria-busy','true');try{const completion=await saveStep();if(index<steps.length-1){index++;render();showSaved(completion)}else{const done=await api('/api/onboarding/steps/review',{method:'PUT',body:JSON.stringify({completed:true})});state=await api('/api/onboarding/state');if(!done.saved||state.steps.review!==true)throw Error('Final setup progress could not be verified.');render();showSaved(completion);$('#notice').prepend(document.createTextNode('Setup complete. Your information stays under your control. '))}}catch(e){$('#notice').className='notice error';$('#notice').textContent='Save failed: '+e.message;flagInvalid(e.message)}finally{button.disabled=false;button.removeAttribute('aria-busy')}};$('#skip').onclick=async()=>{const key=steps[index][0],button=$('#skip');button.disabled=true;button.setAttribute('aria-busy','true');try{const saved=await api('/api/onboarding/steps/'+key,{method:'PUT',body:JSON.stringify({completed:true})});state=await api('/api/onboarding/state');if(!saved.saved||state.steps[key]!==true)throw Error('Skipped progress could not be verified.');const completion=await api('/api/candidate-profile/completeness');if(index<steps.length-1)index++;render();showSaved(completion)}catch(e){$('#notice').className='notice error';$('#notice').textContent='Save failed: '+e.message}finally{button.disabled=false;button.removeAttribute('aria-busy')}};load();
</script></body></html>'''
