"""Tests for cache folder and name patterns (genfill.naming, config getters, state numbers)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_env  # noqa: E402,F401  (temp config dir, never ~/.nuke/genfill)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nuke", "python"))

from genfill import config, naming, state  # noqa: E402

TOKENS = {"script": "sh010_comp_v003", "node": "GenerativeFill1", "uid": "ab12", "user": "cs",
          "gen": 7, "date": "20261015", "time": "120000", "frame": 1001, "backend": "fal",
          "model": "default"}
ENV = {"SHOW": "demo", "SHOT": "sh010", "TASK": "comp"}


class ExpandTests(unittest.TestCase):
    def test_env_vars_in_all_three_spellings(self):
        text, missing = naming.expand("$SHOW/${SHOT}/%TASK%", {}, ENV)
        self.assertEqual(text, "demo/sh010/comp")
        self.assertEqual(missing, [])

    def test_tokens_with_format(self):
        text, _ = naming.expand("{script}_v{gen:03d}_{frame}", TOKENS, {})
        self.assertEqual(text, "sh010_comp_v003_v007_1001")

    def test_env_and_tokens_do_not_interfere(self):
        # "${SHOT}" is an environment variable, never the token {SHOT}.
        text, _ = naming.expand("${SHOT}_{gen:02d}", TOKENS, ENV)
        self.assertEqual(text, "sh010_07")

    def test_unset_env_var_is_reported_and_dropped(self):
        text, missing = naming.expand("${SHOT}_${NOPE}_x", {}, ENV)
        self.assertEqual(text, "sh010__x")
        self.assertEqual(missing, ["NOPE"])

    def test_unknown_token_names_the_known_ones(self):
        with self.assertRaises(naming.PatternError) as cm:
            naming.expand("{shot}", TOKENS, {})
        self.assertIn("{gen}", str(cm.exception))

    def test_bad_format(self):
        with self.assertRaises(naming.PatternError):
            naming.expand("{node:03d}", TOKENS, {})

    def test_uses(self):
        self.assertTrue(naming.uses("x_{gen:04d}", "gen"))
        self.assertTrue(naming.uses("{uid}", "uid"))
        self.assertFalse(naming.uses("${gen}", "gen"))


class NameTests(unittest.TestCase):
    def test_default_name_is_unchanged(self):
        name, _ = naming.generation_name(naming.DEFAULT_NAME, TOKENS, {})
        self.assertEqual(name, "g007_20261015-120000")

    def test_pipeline_name(self):
        name, _ = naming.generation_name("${SHOT}_${TASK}_genfill_v{gen:03d}", TOKENS, ENV)
        self.assertEqual(name, "sh010_comp_genfill_v007")

    def test_number_is_appended_when_missing(self):
        name, _ = naming.generation_name("${SHOT}_genfill", TOKENS, ENV)
        self.assertEqual(name, "sh010_genfill_007")
        var, _ = naming.variation_name("${SHOT}", dict(TOKENS, name="n", var=2), ENV)
        self.assertEqual(var, "sh010_02")

    def test_names_are_made_safe(self):
        name, _ = naming.generation_name("a b/c:{gen}", TOKENS, {})
        self.assertEqual(name, "a_b_c_7")
        name, _ = naming.generation_name("${NOPE}_{gen}", TOKENS, {})
        self.assertEqual(name, "7")

    def test_empty_name_is_an_error(self):
        with self.assertRaises(naming.PatternError):
            naming.variation_name("{var:.0s}", dict(TOKENS, name="", var="x"), {})

    def test_default_variation(self):
        var, _ = naming.variation_name(naming.DEFAULT_VARIATION, dict(TOKENS, name="g", var=3), {})
        self.assertEqual(var, "var_03")


class RootTests(unittest.TestCase):
    def test_absolute_root_with_env(self):
        path, missing = naming.root_path("/jobs/$SHOW/$SHOT/genfill", {"script_dir": "/x"}, ENV)
        self.assertEqual(path, os.path.normpath("/jobs/demo/sh010/genfill"))
        self.assertEqual(missing, [])

    def test_relative_root_is_next_to_the_script(self):
        path, _ = naming.root_path("genfill/${SHOT}", {"script_dir": "/proj/nk"}, ENV)
        self.assertEqual(path, os.path.normpath("/proj/nk/genfill/sh010"))

    def test_empty_root_is_an_error(self):
        with self.assertRaises(naming.PatternError):
            naming.root_path("${NOPE}", {"script_dir": "/x"}, {})


class ConfigTests(unittest.TestCase):
    def test_env_wins_over_config_and_default(self):
        with mock.patch.object(config, "load", return_value={"name_pattern": "cfg_{gen}",
                                                              "variation_pattern": "v{var}"}):
            with mock.patch.dict(os.environ, {"GENFILL_NAME": " env_{gen} ",
                                              "GENFILL_VARIATION_NAME": ""}):
                self.assertEqual(config.name_pattern(), "env_{gen}")
                self.assertEqual(config.variation_pattern(), "v{var}")
        with mock.patch.object(config, "load", return_value={}), \
                mock.patch.dict(os.environ, {"GENFILL_NAME": "", "GENFILL_CACHE": ""}):
            self.assertEqual(config.name_pattern(), naming.DEFAULT_NAME)
            self.assertEqual(config.variation_pattern(), naming.DEFAULT_VARIATION)
            self.assertEqual(config.cache_root_pattern(), "")


class NumberTests(unittest.TestCase):
    def test_numbers_are_never_reused(self):
        st = state.empty()
        self.assertEqual(state.next_generation_number(st), 1)
        st["generations"] = [{"id": "g004_20261012-201500"}, {"id": "sh010_v009", "number": 9}]
        self.assertEqual(state.next_generation_number(st), 10)
        st["generations"] = [{"id": "custom"}]
        self.assertEqual(state.next_generation_number(st), 2)


if __name__ == "__main__":
    unittest.main()
