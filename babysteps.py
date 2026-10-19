import argparse
from candidate_profile import build_profile
from job_finder import find_jobs
from job_matcher import match_jobs


def main():
    parser = argparse.ArgumentParser(description="BabyStepsAI local discovery prototype")
    parser.add_argument("--resume", default="resume.pdf", help="Path to your local resume PDF")
    parser.add_argument("--no-ai", action="store_true")
    args = parser.parse_args()
    print("1/3 Building candidate profile...")
    build_profile(args.resume, use_ai=not args.no_ai)
    print("2/3 Discovering jobs...")
    find_jobs()
    print("3/3 Checking job preferences...")
    match_jobs()
    print("Done. No applications were submitted.")


if __name__ == "__main__":
    main()
