import unittest

from application_browser import apply_targets, classify_control, page_interruption, submission_controls


class ApplicationBrowserSafetyTests(unittest.TestCase):
    def test_next_is_only_safe_with_specific_button_semantics(self):
        self.assertEqual(classify_control("Next", "button", "button"), "safe_next")
        self.assertEqual(classify_control("Continue", "button", "button"), "ambiguous")
        self.assertEqual(classify_control("Continue to education", "button", "button"), "safe_next")
        self.assertEqual(classify_control("Continue", "button", "submit"), "final_submission")

    def test_final_submit_and_send_wording_are_never_navigation(self):
        controls=[{"label":"Send Application","tag":"button","type":"button"},
                  {"label":"Proceed","tag":"button","type":"submit"}]
        self.assertEqual([x["action"] for x in submission_controls(controls)], ["final_submission","final_submission"])

    def test_only_clear_apply_links_are_resolved(self):
        links=[{"label":"Apply Now","href":"/apply"},{"label":"Job details","href":"/details"},
               {"label":"Apply","href":"javascript:submitForm()"}]
        self.assertEqual(apply_targets("https://employer.example/jobs/1",links),["https://employer.example/apply"])

    def test_human_auth_and_expired_pages_are_detected(self):
        self.assertEqual(page_interruption("Please complete CAPTCHA"),"human_verification")
        self.assertEqual(page_interruption("Sign in to continue"),"authentication_required")
        self.assertEqual(page_interruption("Position is no longer available"),"expired_listing")
        self.assertIsNone(page_interruption("Software Engineer opening"))


if __name__ == "__main__":
    unittest.main()
