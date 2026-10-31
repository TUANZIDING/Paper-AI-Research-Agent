import json
import os
import tempfile
import unittest
from pathlib import Path

from ai_research_agent.institutionv04 import (
    PROFILE_ENV,
    ProfileError,
    load_profile,
    parse_profile,
    plan_access,
    read_needs_tsv,
    write_access_outputs,
)

ROOT = Path(__file__).resolve().parents[1]

PROFILE = {
    "institution": "Example University",
    "webvpn_portal": "https://webvpn.example.edu/",
    "document_delivery": "https://library.example.edu/ill",
    "platforms": [
        {"name": "Wiley", "doi_prefixes": ["10.1002", "10.1111/"], "hosts": ["onlinelibrary.wiley.com"],
         "entry_url": "https://onlinelibrary.wiley.com/action/ssostart"},
    ],
}


class InstitutionProfileTests(unittest.TestCase):
    def test_example_profile_in_repo_is_valid_and_credential_free(self):
        profile = load_profile(ROOT / "profiles" / "institution-profile-example-v0.4.json")
        self.assertIn("Example", profile.institution)
        self.assertGreaterEqual(len(profile.platforms), 5)

    def test_rejects_credentials_and_userinfo_urls(self):
        for bad in ({**PROFILE, "password": "x"}, {**PROFILE, "auth": {"Session-Cookie": "x"}},
                    {**PROFILE, "username": "20260000"}):
            with self.assertRaises(ProfileError):
                parse_profile(bad)
        bad_url = json.loads(json.dumps(PROFILE))
        bad_url["platforms"][0]["entry_url"] = "https://user:pw@onlinelibrary.wiley.com/"
        with self.assertRaises(ProfileError):
            parse_profile(bad_url)
        with self.assertRaises(ProfileError):
            parse_profile({"platforms": []})

    def test_routes_by_doi_prefix_host_then_fallbacks(self):
        profile = parse_profile(PROFILE)
        planned = plan_access([
            {"title": "A", "doi": "10.1002/abc", "pmid": "1", "landing_url": ""},
            {"title": "B", "doi": "10.1111/x", "pmid": "", "landing_url": ""},
            {"title": "C", "doi": "", "pmid": "", "landing_url": "https://onlinelibrary.wiley.com/doi/z"},
            {"title": "D", "doi": "10.1667/rade", "pmid": "", "landing_url": ""},
            {"title": "E", "doi": "10.10021/not-wiley", "pmid": "", "landing_url": ""},
        ], profile)
        self.assertEqual([p.route for p in planned],
                         ["federated_login", "federated_login", "federated_login", "webvpn_then_doi", "webvpn_then_doi"])
        self.assertEqual(planned[0].landing_url, "https://doi.org/10.1002/abc")
        no_vpn = parse_profile({k: v for k, v in PROFILE.items() if k != "webvpn_portal"})
        self.assertEqual(plan_access([{"title": "D", "doi": "10.1667/r"}], no_vpn)[0].route, "document_delivery")
        self.assertEqual(plan_access([{"title": "D", "doi": "10.1667/r"}], None)[0].route, "no_profile")

    def test_outputs_are_manual_checklist_and_rerunnable(self):
        profile = parse_profile(PROFILE)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rows = [{"title": "Muscle\tinjury <b>", "doi": "10.1002/abc", "pmid": "9", "landing_url": ""}]
            counts = write_access_outputs(out, rows, profile)
            self.assertEqual(counts, {"federated_login": 1})
            page = (out / "institutional-access-v0.4.html").read_text(encoding="utf-8")
            self.assertIn("Do not use scripts or download tools", page)
            self.assertIn("&lt;b&gt;", page)
            again = read_needs_tsv(out / "needs-institutional-access-v0.4.tsv")
            self.assertEqual(again[0]["route"], "federated_login")
            self.assertEqual(write_access_outputs(out, again, profile), {"federated_login": 1})

    def test_env_variable_and_unset(self):
        old = os.environ.pop(PROFILE_ENV, None)
        try:
            self.assertIsNone(load_profile())
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "p.json"
                path.write_text(json.dumps(PROFILE), encoding="utf-8")
                os.environ[PROFILE_ENV] = str(path)
                self.assertEqual(load_profile().institution, "Example University")
        finally:
            os.environ.pop(PROFILE_ENV, None)
            if old is not None:
                os.environ[PROFILE_ENV] = old


if __name__ == "__main__":
    unittest.main()
