import unittest

from application_browser import (apply_targets, classify_control, detect_application_provider,
                                 page_interruption, submission_controls, summarize_form_metadata)


class ApplicationBrowserSafetyTests(unittest.TestCase):
    def test_provider_detection_is_conservative_and_exposes_limited_coverage(self):
        self.assertEqual(detect_application_provider("https://jobs.ashbyhq.com/example/123")["provider"],"ashby")
        self.assertEqual(detect_application_provider("https://boards.greenhouse.io/example/jobs/1")["provider"],"greenhouse")
        self.assertEqual(detect_application_provider("https://jobs.lever.co/example/1")["provider"],"lever")
        self.assertEqual(detect_application_provider("https://jobs.smartrecruiters.com/Example/1")["support_level"],"limited_public_layout")
        self.assertEqual(detect_application_provider("https://example.myworkdayjobs.com/en-US/jobs/1")["support_level"],"limited_public_layout")
        self.assertEqual(detect_application_provider("https://careers.example.test/jobs/1")["provider"],"employer_site")

    def test_next_is_only_safe_with_specific_button_semantics(self):
        self.assertEqual(classify_control("Next", "button", "button"), "safe_next")
        self.assertEqual(classify_control("Continue", "button", "button"), "ambiguous")
        self.assertEqual(classify_control("Continue to education", "button", "button"), "safe_next")
        self.assertEqual(classify_control("Continue", "button", "submit", inside_form=True), "ambiguous")

    def test_final_submit_and_send_wording_are_never_navigation(self):
        controls=[{"label":"Send Application","tag":"button","type":"button"},
                  {"label":"Proceed","tag":"button","type":"submit"}]
        self.assertEqual([x["action"] for x in submission_controls(controls)], ["final_submission","ambiguous"])

    def test_final_submission_report_preserves_safe_boundary_metadata(self):
        actions=submission_controls([{"label":"Submit Application","tag":"button","type":"submit",
            "inside_form":True,"visible":True,"disabled":False,"form_id":"application_form",
            "form_action_path":"/applications/submit","form_method":"post","aria_label":"Submit Application"}])
        self.assertEqual(len(actions),1)
        self.assertEqual(actions[0]["action"],"final_submission")
        self.assertEqual(actions[0]["type"],"submit")
        self.assertEqual(actions[0]["form_action_path"],"/applications/submit")
        self.assertTrue(actions[0]["enabled"])

    def test_apply_now_and_unlabeled_submit_are_unresolved_not_final(self):
        controls=[{"label":"Apply Now","tag":"button","type":"submit","inside_form":True},
                  {"label":"","tag":"button","type":"submit","inside_form":True}]
        self.assertEqual([item["action"] for item in submission_controls(controls)], ["ambiguous","ambiguous"])

    def test_upload_buttons_are_never_submission_controls(self):
        self.assertEqual(submission_controls([{"label":"Attach","tag":"button","type":"submit","inside_form":True}]),[])

    def test_cookie_and_pagination_submit_buttons_are_not_application_submissions(self):
        controls=[{"label":"Accept","tag":"button","type":"submit"},
                  {"label":"Accept All","tag":"button","type":"button"},
                  {"label":"Necessary Only","tag":"button","type":"button"},
                  {"label":"Customize Settings","tag":"button","type":"button"},
                  {"label":"Create alert","tag":"button","type":"submit"},
                  {"label":"2","tag":"button","type":"submit"}]
        self.assertEqual(submission_controls(controls),[])

    def test_only_clear_apply_links_are_resolved(self):
        links=[{"label":"Apply Now","href":"/apply"},{"label":"Job details","href":"/details"},
               {"label":"Apply","href":"javascript:submitForm()"}]
        self.assertEqual(apply_targets("https://employer.example/jobs/1",links),["https://employer.example/apply"])

    def test_human_auth_and_expired_pages_are_detected(self):
        self.assertEqual(page_interruption("Please complete CAPTCHA"),"human_verification")
        self.assertEqual(page_interruption("Sign in to continue"),"authentication_required")
        self.assertEqual(page_interruption("We use cookies. Accept All or Necessary Only."),"cookie_preference_required")
        self.assertEqual(page_interruption("Position is no longer available"),"expired_listing")
        self.assertIsNone(page_interruption("Software Engineer opening"))

    def test_compatibility_report_covers_fields_uploads_steps_and_submission_without_values(self):
        report=summarize_form_metadata(
            detect_application_provider("https://jobs.ashbyhq.com/example/job"),
            [{"label":"Email","kind":"email","required":True},
             {"label":"Resume","kind":"file","required":True,"accept":".pdf"},
             {"label":"Work location","kind":"custom-widget","required":False}],
            [{"label":"Next","tag":"button","type":"button","action":"safe_next"},
             {"label":"Submit application","tag":"button","type":"submit","action":"final_submission"}])
        self.assertEqual(report["provider"]["provider"],"ashby")
        self.assertTrue(report["form_detected"])
        self.assertEqual(report["inspection_outcome"],"partially_compatible")
        self.assertEqual(report["required_fields"],["Email","Resume"])
        self.assertEqual(report["file_upload_controls"],["Resume"])
        self.assertTrue(report["multi_step_detected"])
        self.assertTrue(report["submission_control_detected"])
        self.assertEqual(report["unsupported_fields"],["Work location"])
        self.assertNotIn("value",str(report).casefold())

    def test_compatibility_report_flags_human_actions_and_missing_forms(self):
        captcha=summarize_form_metadata(detect_application_provider("https://jobs.lever.co/example/job"),[],[],interruption="human_verification")
        self.assertEqual(captcha["inspection_outcome"],"needs_human_action")
        self.assertIn("CAPTCHA",captcha["human_actions_required"][0])
        no_form=summarize_form_metadata(detect_application_provider("https://example.com/jobs/1"),[],[])
        self.assertEqual(no_form["inspection_outcome"],"unsupported")
        self.assertFalse(no_form["form_detected"])

    def test_ashby_greenhouse_and_lever_fixture_reports_are_provider_aware(self):
        fields=[{"label":"Location","kind":"combobox","required":True},
                {"label":"Resume","kind":"file","required":True,"accept":".pdf"},
                {"label":"Eligibility","kind":"radio","required":True},
                {"label":"Consent preference","kind":"checkbox","required":False},
                {"label":"Comments","kind":"textarea","required":False}]
        urls=[("ashby","https://jobs.ashbyhq.com/acme/job"),
              ("greenhouse","https://boards.greenhouse.io/acme/jobs/1"),
              ("lever","https://jobs.lever.co/acme/job")]
        for expected,url in urls:
            report=summarize_form_metadata(detect_application_provider(url),fields,
                [{"label":"Next","tag":"button","type":"button","action":"safe_next"},
                 {"label":"Submit application","tag":"button","type":"submit","action":"final_submission"}])
            self.assertEqual(report["provider"]["provider"],expected)
            self.assertEqual(report["inspection_outcome"],"compatible")
            self.assertEqual(len(report["fields"]),5)
            self.assertEqual(report["required_fields"],["Location","Resume","Eligibility"])
            self.assertEqual(report["file_upload_controls"],["Resume"])
            self.assertTrue(report["multi_step_detected"])
            self.assertTrue(report["submission_control_detected"])

    def test_greenhouse_ziprecruiter_sanitized_structure_groups_duplicate_controls(self):
        # Sanitized structural fixture from the public Greenhouse page. It
        # includes visible question labels, repeated implementation controls,
        # and no applicant-entered values.
        fields=[]
        def add(question,kind="text",required=True,**extra):
            fields.append({"question":question,"label":question,"kind":kind,"required":required,**extra})
        add("First Name*"); add("Last Name*"); add("Email*","email"); add("Country*")
        add("Phone","text"); add("Phone*","tel")
        add("Location (City)*"); add("Location (City)*","combobox")
        add("School*"); add("School*","combobox")
        add("Degree*"); add("Degree*","combobox")
        add("What archetype are you most closely aligned with?*","text",possible_custom_selector=True)
        add("What archetype are you most closely aligned with?*","combobox")
        add("What school are you currently attending / did you graduate from?*")
        add("When is your anticipated graduation date?*"); add("When is your anticipated graduation date?*","combobox")
        add("Are you open to relocation to Santa Monica, CA?*","text",possible_custom_selector=True)
        add("Are you open to relocation to Santa Monica, CA?*","combobox")
        add("Are you legally authorized to work in the United States for our Company?*","text",possible_custom_selector=True)
        add("Are you legally authorized to work in the United States for our Company?*","combobox")
        add("Will you now, or in the future, require sponsorship to legally work in the U.S.?*","text",possible_custom_selector=True)
        add("Will you now, or in the future, require sponsorship to legally work in the U.S.?*","combobox")
        add("Resume/CV*","file",True,upload_purpose="resume",accept=".pdf,.doc,.docx,.txt,.rtf")
        add("How did you hear about this opportunity?","text",False)
        add("Gender","text",False); add("Are you Hispanic/Latino?","combobox",False); add("Veteran Status","combobox",False)
        controls=[{"label":"Submit application","tag":"button","type":"submit","action":"final_submission","visible":True}]
        report=summarize_form_metadata(detect_application_provider("https://job-boards.greenhouse.io/ziprecruiter/jobs/8127108"),fields,controls,interruption="human_verification")
        self.assertEqual(report["provider"]["provider"],"greenhouse")
        self.assertEqual(report["required_control_count"],24)
        self.assertEqual(report["unique_required_question_count"],15)
        self.assertEqual(report["unique_optional_question_count"],4)
        self.assertEqual(report["required_fields"], [q["question"] for q in report["required_questions"]])
        self.assertEqual(report["file_upload_controls"],["Resume/CV*"])
        self.assertEqual(report["fields"][23]["upload_purpose"],"resume")
        phone=next(item for item in report["questions"] if item["question"].startswith("Phone"))
        self.assertEqual(set(phone["control_types"]),{"text","tel"})
        self.assertTrue(phone["question_id"])
        self.assertGreater(report["total_dom_control_count"],report["visible_form_control_count"])
        self.assertTrue(report["submission_control_detected"])
        self.assertEqual(report["submission_control_status"],"detected_visible")
        self.assertIn("CAPTCHA or human verification",report["human_actions_required"])
        self.assertNotIn("Gender",report["required_fields"])
        self.assertGreater(report["unresolved_required_control_count"],0)
        self.assertEqual(report["inspection_outcome"],"needs_human_action")

    def test_greenhouse_report_keeps_missing_submit_detection_unresolved(self):
        report=summarize_form_metadata(detect_application_provider("https://job-boards.greenhouse.io/example/jobs/1"),
            [{"question":"Resume","label":"Resume","kind":"file","required":True}],[])
        self.assertFalse(report["submission_control_detected"])
        self.assertEqual(report["submission_control_status"],"not_detected_unresolved")
        self.assertEqual(report["inspection_outcome"],"partially_compatible")
        self.assertEqual(report["compatibility_status"],"unresolved")
        self.assertTrue(report["form_fingerprint"])

    def test_upload_button_without_exposed_file_input_is_unresolved_not_verified_upload(self):
        report=summarize_form_metadata(detect_application_provider("https://job-boards.greenhouse.io/example/jobs/1"),
            [{"question":"Email*","label":"Email*","kind":"email","required":True}],
            [{"label":"Attach","tag":"button","type":"button","inside_form":True,"upload_prompt":True,
              "upload_question":"Resume/CV*","upload_purpose":"resume","upload_required":True},
             {"label":"Submit application","tag":"button","type":"submit","inside_form":True,"action":"final_submission","visible":True}])
        self.assertEqual(report["file_upload_controls"],[])
        prompt=report["upload_prompts"][0]
        self.assertEqual({key:prompt[key] for key in ("question","purpose","required","verified_input")},
            {"question":"Resume/CV*","purpose":"resume","required":True,"verified_input":False})
        self.assertFalse(prompt["supported"])
        self.assertTrue(prompt["unresolved_reason"])
        self.assertIn("Resume/CV*",report["required_fields"])
        self.assertEqual(report["unique_required_question_count"],2)
        self.assertEqual(report["compatibility_status"],"unresolved")
        self.assertTrue(any(item["kind"]=="file_upload_prompt" for item in report["unresolved_controls"]))

    def test_greenhouse_hidden_canonical_file_inputs_are_associated_without_extra_questions(self):
        fields=[{"__inspection_meta__":True,"ignored_controls":[
            {"tag":"input","input_type":"file","id":"resume","required":False,"reason":"zero_size","accept":".pdf,.docx"},
            {"tag":"input","input_type":"file","id":"cover_letter","required":False,"reason":"zero_size","accept":".pdf,.docx"}]},
            {"question":"Email*","label":"Email*","kind":"email","required":True}]
        controls=[{"label":"Attach","tag":"button","type":"button","inside_form":True,"upload_prompt":True,
            "upload_question":"Resume/CV*","upload_purpose":"resume","upload_required":True},
            {"label":"Attach","tag":"button","type":"button","inside_form":True,"upload_prompt":True,
            "upload_question":"Cover Letter","upload_purpose":"cover_letter","upload_required":False},
            {"label":"Submit Application","tag":"button","type":"submit","inside_form":True,
            "action":"final_submission","visible":True,"disabled":False}]
        report=summarize_form_metadata(detect_application_provider("https://job-boards.greenhouse.io/example/jobs/1"),fields,controls)
        self.assertEqual(report["unique_question_count"],3)
        self.assertEqual(report["unique_required_question_count"],2)
        self.assertEqual(report["unique_optional_question_count"],1)
        self.assertEqual(report["file_upload_controls"],["Resume/CV*","Cover Letter"])
        by_question={item["question"]:item for item in report["upload_control_details"]}
        self.assertEqual(by_question["Resume/CV*"]["association_method"],"provider_field_identifier")
        self.assertEqual(by_question["Resume/CV*"]["mechanism"],"hidden_native_input")
        self.assertTrue(by_question["Resume/CV*"]["supported"])
        self.assertEqual(by_question["Resume/CV*"]["accepted_types"],[".pdf",".docx"])
        self.assertEqual(by_question["Cover Letter"]["input_id"],"cover_letter")
        self.assertFalse(any(item["kind"]=="file_upload_prompt" for item in report["unresolved_controls"]))
        self.assertEqual(report["compatibility_status"],"compatible")

    def test_unmatched_required_upload_remains_blocking(self):
        report=summarize_form_metadata(detect_application_provider("https://job-boards.greenhouse.io/example/jobs/1"),
            [{"__inspection_meta__":True,"ignored_controls":[{"tag":"input","input_type":"file","id":"attachment_4","required":False,"reason":"zero_size"}]},
             {"question":"Email*","label":"Email*","kind":"email","required":True}],
            [{"label":"Attach","tag":"button","type":"button","inside_form":True,"upload_prompt":True,
              "upload_question":"Transcript*","upload_purpose":"transcript","upload_required":True},
             {"label":"Submit Application","tag":"button","type":"submit","inside_form":True,"action":"final_submission","visible":True}])
        self.assertEqual(report["upload_control_details"][0]["supported"],False)
        self.assertEqual(report["unresolved_required_control_count"],1)
        self.assertEqual(report["compatibility_status"],"unresolved")

    def test_hidden_file_input_is_associated_by_accessible_label(self):
        report=summarize_form_metadata(detect_application_provider("https://job-boards.greenhouse.io/example/jobs/1"),
            [{"__inspection_meta__":True,"ignored_controls":[{"tag":"input","input_type":"file","id":"upload-17",
                "label_text":"Resume/CV*","accept":"application/pdf","reason":"zero_size"}]},
             {"question":"Email*","label":"Email*","kind":"email","required":True}],
            [{"label":"Attach","tag":"button","type":"button","inside_form":True,"upload_prompt":True,
              "upload_question":"Resume/CV*","upload_purpose":"resume","upload_required":True},
             {"label":"Submit Application","tag":"button","type":"submit","inside_form":True,"action":"final_submission"}])
        detail=report["upload_control_details"][0]
        self.assertEqual(detail["association_method"],"accessible_label")
        self.assertEqual(detail["accepted_types"],["application/pdf"])

    def test_unknown_requiredness_is_not_reported_as_optional(self):
        report=summarize_form_metadata(detect_application_provider("https://jobs.ashbyhq.com/example/job"),
            [{"question":"Unclear question","label":"Unclear question","kind":"text","required":None}],
            [{"label":"Submit application","tag":"button","type":"submit","inside_form":True,"action":"final_submission","visible":True}])
        self.assertEqual(report["unique_required_question_count"],0)
        self.assertEqual(report["unique_optional_question_count"],0)
        self.assertEqual(report["unique_unknown_requirement_question_count"],1)
        self.assertEqual(report["fields"][0]["required"],"unknown")
        self.assertEqual(report["compatibility_status"],"unresolved")

    def test_ambiguous_apply_button_is_not_misreported_as_final_submit(self):
        report=summarize_form_metadata(detect_application_provider("https://jobs.lever.co/example/job"),
            [{"question":"Email*","label":"Email*","kind":"email","required":True}],
            [{"label":"Apply now","tag":"button","type":"button","inside_form":True,"action":"ambiguous","visible":True}])
        self.assertFalse(report["submission_control_detected"])
        self.assertEqual(report["submission_control_status"],"ambiguous_unresolved")
        self.assertEqual(report["compatibility_status"],"unresolved")
        self.assertFalse(report["multi_step_detected"])

    def test_hidden_implementation_controls_are_separated_from_question_counts(self):
        report=summarize_form_metadata(detect_application_provider("https://job-boards.greenhouse.io/example/jobs/1"),[
            {"__inspection_meta__":True,"ignored_controls":[{"tag":"input","input_type":"text","reason":"aria_hidden"}]},
            {"question":"Email*","label":"Email*","kind":"email","required":True}],[])
        self.assertEqual(report["unique_required_question_count"],1)
        self.assertEqual(report["ignored_control_count"],1)
        self.assertEqual(report["ignored_controls"][0]["reason"],"aria_hidden")


if __name__ == "__main__":
    unittest.main()
