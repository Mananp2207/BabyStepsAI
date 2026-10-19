
import json
from playwright.sync_api import sync_playwright


def inspect_form(page):
    fields = page.locator(
        "input, textarea, select"
    )

    results = []

    for i in range(fields.count()):
        field = fields.nth(i)

        if not field.is_visible():
            continue

        info = {
            "tag": field.evaluate(
                "(el) => el.tagName.toLowerCase()"
            ),
            "type": field.get_attribute("type") or "",
            "name": field.get_attribute("name") or "",
            "id": field.get_attribute("id") or "",
            "placeholder": (
                field.get_attribute("placeholder") or ""
            ),
            "required": field.get_attribute(
                "required"
            ) is not None,
            "label": ""
        }

        field_id = info["id"]

        if field_id:
            label = page.locator(
                f'label[for="{field_id}"]'
            )

            if label.count() > 0:
                info["label"] = label.first.inner_text()

        results.append(info)

    return results


def main():
    url = input("Enter job application URL: ").strip()

    if not url.startswith(("http://", "https://")):
        print("Please enter a valid website URL.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False
        )

        page = browser.new_page()
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        print("Inspecting application form...")

        fields = inspect_form(page)

        print(json.dumps(fields, indent=4))

        with open(
            "detected_fields.json",
            "w"
        ) as file:
            json.dump(fields, file, indent=4)

        print("Saved: detected_fields.json")

        input("Press Enter to close browser...")
        browser.close()


if __name__ == "__main__":
    main()
