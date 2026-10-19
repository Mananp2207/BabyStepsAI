
def fill_application(page, candidate):
    """Legacy deterministic field helper; caller must supply verified facts."""
    fields = page.locator("input")

    for i in range(fields.count()):
        field = fields.nth(i)

        if not field.is_visible():
            continue

        name = (field.get_attribute("name") or "").lower()
        placeholder = (
            field.get_attribute("placeholder") or ""
        ).lower()

        info = name + " " + placeholder

        if "email" in info:
            value = candidate["email"]
        elif "github" in info:
            value = candidate["github"]
        elif "phone" in info or "mobile" in info:
            value = candidate["phone"]
        elif "name" in info:
            value = candidate["full_name"]
        else:
            print("Unknown field:", info)
            continue

        if not value:
            print("Missing value:", name)
            continue

        field.fill(value)
        print("Filled:", name)


def main():
    print("Use BabyStepsAI's /applications dashboard for isolated, verified-profile dry runs.")
    print("Direct browser runs are disabled here to avoid reading a hardcoded personal resume or submitting a form.")


if __name__ == "__main__":
    main()
