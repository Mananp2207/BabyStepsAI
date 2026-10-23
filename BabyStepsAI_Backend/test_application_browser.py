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
        self.assertEqual(classify_control("Continue", "button", "submit"), "final_submission")

    def test_final_submit_and_send_wording_are_never_navigation(self):
        controls=[{"label":"Send Application","tag":"button","type":"button"},
                  {"label":"Proceed","tag":"button","type":"submit"}]
        self.assertEqual([x["action"] for x in submission_controls(controls)], ["final_submission","final_submission"])

    def test_cookie_and_pagination_submit_buttons_are_not_application_submissions(self):
        controls=[{"label":"Accept","tag":"button","type":"submit"},
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


if __name__ == "__main__":
    unittest.main()
