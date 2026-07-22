import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import ugc_import
from app.services.ugc_brief import generate_brief
from app.services.ugc_compliance import disclosure_enabled, platform_guidance
from app.services.ugc_creative import generate_ctas, generate_hooks, generate_scripts
from app.services.ugc_credits import _current_period, credit_cost_per_variation
from app.services.ugc_variation_engine import sample_combos
from app.services import ugc_learner
from app.services.ugc_platforms import get_preset, list_presets, max_length_for
from app.ugc_providers import get_avatar_provider, get_voice_provider
from app.ugc_providers.base import VoiceSpec
from app.ugc_providers.heygen import HeyGenAvatarProvider


def _resp(*, json_data=None, text=""):
    return SimpleNamespace(json=lambda: json_data, text=text, raise_for_status=lambda: None)


class SourceDetectionTests(unittest.TestCase):
    def test_detects_sources(self) -> None:
        self.assertEqual(ugc_import.detect_source_type("https://apps.apple.com/us/app/x/id123"), "app_store")
        self.assertEqual(ugc_import.detect_source_type("https://play.google.com/store/apps/details?id=a.b"), "play")
        self.assertEqual(ugc_import.detect_source_type("https://shop.myshopify.com/products/serum"), "shopify")
        self.assertEqual(ugc_import.detect_source_type("https://brand.com/products/serum"), "shopify")
        self.assertEqual(ugc_import.detect_source_type("https://brand.com/landing"), "landing")


class OgJsonLdTests(unittest.TestCase):
    def test_extract_og(self) -> None:
        html = (
            '<meta property="og:title" content="Acne Serum">'
            '<meta name="description" content="Clears skin">'
            '<meta property="og:image" content="https://x/img.jpg">'
            '<meta property="product:price:amount" content="29.99">'
            '<meta property="product:price:currency" content="USD">'
        )
        og = ugc_import._extract_og(html)
        self.assertEqual(og["title"], "Acne Serum")
        self.assertEqual(og["description"], "Clears skin")
        self.assertEqual(og["image"], "https://x/img.jpg")
        self.assertEqual(og["price_amount"], "29.99")
        self.assertEqual(og["price_currency"], "USD")

    def test_extract_jsonld_product(self) -> None:
        html = (
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"Serum","description":"d",'
            '"offers":{"price":"19.0","priceCurrency":"USD"},'
            '"review":[{"reviewBody":"loved it"}]}'
            "</script>"
        )
        ld = ugc_import._extract_jsonld_product(html)
        self.assertIsNotNone(ld)
        self.assertEqual(ld["name"], "Serum")
        self.assertEqual(ugc_import._jsonld_reviews(ld), ["loved it"])

    def test_jsonld_graph(self) -> None:
        html = (
            '<script type="application/ld+json">'
            '{"@graph":[{"@type":"WebSite"},{"@type":["Product"],"name":"G"}]}'
            "</script>"
        )
        ld = ugc_import._extract_jsonld_product(html)
        self.assertEqual(ld["name"], "G")


class ExtractorTests(unittest.TestCase):
    def test_shopify(self) -> None:
        product = {
            "product": {
                "title": "Clarity Serum",
                "vendor": "GlowLab",
                "handle": "clarity-serum",
                "body_html": "<p>Clears <b>breakouts</b></p>",
                "variants": [{"price": "29.99"}],
                "images": [{"src": "https://x/1.jpg"}],
            }
        }
        with patch.object(ugc_import, "_get", return_value=_resp(json_data=product)):
            data = ugc_import._shopify("https://brand.com/products/clarity-serum")
        self.assertEqual(data["name"], "Clarity Serum")
        self.assertEqual(data["brand"], "GlowLab")
        self.assertEqual(data["price"], "29.99")
        self.assertIn("Clears", data["description"])
        self.assertEqual(data["image_urls"], ["https://x/1.jpg"])

    def test_app_store(self) -> None:
        lookup = {"results": [{"trackName": "FitApp", "sellerName": "Acme", "price": 0.0,
                                "currency": "USD", "description": "Train daily",
                                "screenshotUrls": ["https://x/s1.png"]}]}
        with patch.object(ugc_import, "_get", return_value=_resp(json_data=lookup)):
            data = ugc_import._app_store("https://apps.apple.com/us/app/fitapp/id123456789")
        self.assertEqual(data["name"], "FitApp")
        self.assertEqual(data["brand"], "Acme")
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["image_urls"], ["https://x/s1.png"])

    def test_app_store_requires_id(self) -> None:
        with self.assertRaises(ValueError):
            ugc_import._app_store("https://apps.apple.com/us/app/noid")

    def test_import_product_dispatch_and_ai_safe(self) -> None:
        product = {"product": {"title": "Serum", "variants": [{"price": "9"}], "images": []}}
        with patch.object(ugc_import, "_get", return_value=_resp(json_data=product)), patch(
            "app.services.ai_client.generate_json", side_effect=RuntimeError("no key")
        ):
            data = ugc_import.import_product("https://brand.com/products/serum")
        self.assertEqual(data["source_type"], "shopify")
        self.assertEqual(data["name"], "Serum")
        # AI enrichment failure must not break import; list fields stay lists.
        for k in ("benefits", "pain_points", "use_cases", "reviews", "image_urls"):
            self.assertIsInstance(data[k], list)


class CreativeFallbackTests(unittest.TestCase):
    """With the model unavailable, generators must still return usable creative."""

    def setUp(self) -> None:
        self.brief = {"audience": "acne-prone 18-30", "main_promise": "clear skin",
                      "benefits": ["fast"], "pain_points": ["stubborn acne"], "angles": ["testimonial"]}
        self.product = {"name": "Clarity Serum"}

    def test_hooks_fallback(self) -> None:
        with patch("app.services.ai_client.generate_json", side_effect=RuntimeError("x")):
            hooks = generate_hooks(self.brief, self.product, 20)
        self.assertTrue(hooks)
        self.assertTrue(all(isinstance(h, str) and h for h in hooks))

    def test_scripts_fallback(self) -> None:
        with patch("app.services.ai_client.generate_json", side_effect=RuntimeError("x")):
            scripts = generate_scripts(self.brief, self.product, ["testimonial", "founder-story"], 5)
        self.assertTrue(scripts)
        self.assertTrue(all("script" in s and "angle" in s for s in scripts))

    def test_ctas_fallback(self) -> None:
        with patch("app.services.ai_client.generate_json", side_effect=RuntimeError("x")):
            ctas = generate_ctas(self.brief, self.product, 3)
        self.assertTrue(ctas)

    def test_brief_fallback(self) -> None:
        product = {"name": "Serum", "description": "good", "benefits": ["b"], "pain_points": ["p"]}
        with patch("app.services.ai_client.generate_json", side_effect=RuntimeError("x")):
            b = generate_brief(product)
        for key in ("audience", "main_promise", "pain_points", "objections", "benefits", "angles"):
            self.assertIn(key, b)
        self.assertEqual(b["benefits"], ["b"])


class ProviderRegistryTests(unittest.TestCase):
    def test_default_is_stub(self) -> None:
        with patch.dict(os.environ, {"UGC_AVATAR_PROVIDER": "", "UGC_RENDER_DRY_RUN": ""}, clear=False):
            self.assertEqual(get_avatar_provider().name, "stub")

    def test_dry_run_forces_stub(self) -> None:
        with patch.dict(os.environ, {"UGC_AVATAR_PROVIDER": "heygen", "UGC_RENDER_DRY_RUN": "1"}, clear=False):
            self.assertEqual(get_avatar_provider().name, "stub")

    def test_heygen_without_key_raises(self) -> None:
        with patch.dict(os.environ, {"UGC_AVATAR_PROVIDER": "heygen", "UGC_RENDER_DRY_RUN": "", "HEYGEN_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                get_avatar_provider("heygen")

    def test_stub_catalog_nonempty(self) -> None:
        self.assertTrue(get_avatar_provider("stub").list_avatars())
        self.assertTrue(get_voice_provider("stub").list_voices())

    def test_registry_selects_heygen_with_key(self) -> None:
        with patch.dict(
            os.environ,
            {"UGC_AVATAR_PROVIDER": "heygen", "UGC_RENDER_DRY_RUN": "", "HEYGEN_API_KEY": "k"},
            clear=False,
        ):
            self.assertEqual(get_avatar_provider().name, "heygen")


class HeyGenAdapterTests(unittest.TestCase):
    def _prov(self) -> HeyGenAvatarProvider:
        with patch.dict(os.environ, {"HEYGEN_API_KEY": "k", "HEYGEN_TEST_MODE": ""}, clear=False):
            return HeyGenAvatarProvider()

    def test_requires_key(self) -> None:
        with patch.dict(os.environ, {"HEYGEN_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                HeyGenAvatarProvider()

    def test_list_avatars_mapping(self) -> None:
        p = self._prov()
        payload = {"data": {"avatars": [{"avatar_id": "av1", "avatar_name": "Ann", "gender": "female", "preview_image_url": "u"}]}}
        with patch.object(p, "_get", return_value=payload):
            specs = p.list_avatars()
        self.assertEqual(specs[0].provider_avatar_id, "av1")
        self.assertEqual(specs[0].name, "Ann")
        self.assertEqual(specs[0].gender_presentation, "female")

    def test_list_voices_mapping(self) -> None:
        p = self._prov()
        payload = {"data": {"voices": [{"voice_id": "v1", "name": "V", "language": "English", "gender": "female"}]}}
        with patch.object(p, "_get", return_value=payload):
            specs = p.list_voices()
        self.assertEqual(specs[0].provider_voice_id, "v1")

    def test_start_render_body_and_video_id(self) -> None:
        p = self._prov()
        captured: dict = {}

        def fake_post(path, body):
            captured["path"] = path
            captured["body"] = body
            return {"data": {"video_id": "vid123"}, "error": None}

        with patch.object(p, "_post", side_effect=fake_post):
            job = p.start_render(script="hi", avatar_id="av1", voice_id="v1", aspect_ratio="9:16", length_sec=30)
        self.assertEqual(job.provider_job_id, "vid123")
        self.assertEqual(job.status, "processing")
        self.assertEqual(captured["body"]["video_inputs"][0]["character"]["avatar_id"], "av1")
        self.assertEqual(captured["body"]["video_inputs"][0]["voice"]["voice_id"], "v1")
        self.assertEqual(captured["body"]["dimension"], {"width": 720, "height": 1280})

    def test_start_render_falls_back_to_default_voice(self) -> None:
        p = self._prov()
        with patch.object(p, "list_voices", return_value=[VoiceSpec("vd", "Default", language="en")]), patch.object(
            p, "_post", return_value={"data": {"video_id": "x"}}
        ) as mp:
            p.start_render(script="hi", avatar_id="av1", voice_id="", aspect_ratio="1:1")
        body = mp.call_args.args[1]
        self.assertEqual(body["video_inputs"][0]["voice"]["voice_id"], "vd")
        self.assertEqual(body["dimension"], {"width": 1080, "height": 1080})

    def test_poll_status_mapping(self) -> None:
        p = self._prov()
        with patch.object(p, "_get", return_value={"data": {"status": "completed", "video_url": "http://x/v.mp4"}}):
            s = p.poll("vid")
            self.assertEqual(s.status, "done")
            self.assertEqual(s.video_url, "http://x/v.mp4")
        with patch.object(p, "_get", return_value={"data": {"status": "processing"}}):
            self.assertEqual(p.poll("vid").status, "processing")
        with patch.object(p, "_get", return_value={"data": {"status": "failed", "error": "boom"}}):
            s = p.poll("vid")
            self.assertEqual(s.status, "failed")
            self.assertIn("boom", s.error or "")
        with patch.object(p, "_get", return_value={"data": {"status": "completed"}}):
            self.assertEqual(p.poll("vid").status, "processing")  # url not propagated yet


class SampleCombosTests(unittest.TestCase):
    def _pools(self):
        return dict(
            scripts=[{"angle": a, "hook": f"h{a}", "script": f"s{a}"} for a in ("ps", "test", "founder")],
            hooks=["h1", "h2", "h3", "h4", "h5"],
            ctas=["Buy now", "Tap link"],
            avatars=[{"provider_avatar_id": f"a{i}", "name": f"A{i}", "default_voice_id": "v"} for i in range(5)],
            voices=["v1", "v2"],
            lengths=[15, 30],
            aspects=["9:16", "1:1"],
            caption_styles=[None],
        )

    def test_count_and_uniqueness(self) -> None:
        combos = sample_combos(count=10, **self._pools())
        self.assertEqual(len(combos), 10)
        keys = {(c["hook"], c["script"], c["avatar"]["provider_avatar_id"], c["voice_id"], c["cta"], c["length"], c["aspect"]) for c in combos}
        self.assertEqual(len(keys), 10)  # all distinct

    def test_caps_at_available_unique(self) -> None:
        combos = sample_combos(
            count=50, scripts=[{"angle": "ps", "hook": "h", "script": "s"}], hooks=["h"],
            ctas=[None], avatars=[{"provider_avatar_id": "a", "name": "A"}], voices=[],
            lengths=[30], aspects=["9:16"], caption_styles=[None],
        )
        self.assertEqual(len(combos), 1)  # only one unique combination exists

    def test_diversity_spans_angles(self) -> None:
        combos = sample_combos(count=3, **self._pools())
        self.assertGreaterEqual(len({c["angle"] for c in combos}), 2)


class CreditsHelperTests(unittest.TestCase):
    def test_cost_default(self) -> None:
        with patch.dict(os.environ, {"UGC_CREDIT_COST_PER_VARIATION": ""}, clear=False):
            os.environ.pop("UGC_CREDIT_COST_PER_VARIATION", None)
            self.assertEqual(credit_cost_per_variation(), 1)

    def test_cost_override(self) -> None:
        with patch.dict(os.environ, {"UGC_CREDIT_COST_PER_VARIATION": "4"}, clear=False):
            self.assertEqual(credit_cost_per_variation(), 4)

    def test_period_format(self) -> None:
        self.assertRegex(_current_period(), r"^\d{4}-\d{2}$")


class ComplianceTests(unittest.TestCase):
    def test_disclosure_default_on(self) -> None:
        self.assertTrue(disclosure_enabled(None))
        self.assertTrue(disclosure_enabled({}))
        self.assertTrue(disclosure_enabled({"ugc_disclosure": True}))

    def test_disclosure_can_disable(self) -> None:
        self.assertFalse(disclosure_enabled({"ugc_disclosure": False}))

    def test_platform_guidance(self) -> None:
        for plat in ("tiktok", "meta", "reels", "shorts", "unknown"):
            self.assertIsInstance(platform_guidance(plat), str)
            self.assertTrue(platform_guidance(plat))


class QueueGuardTests(unittest.TestCase):
    def test_render_enqueue_without_redis_returns_none(self) -> None:
        from app.jobs.queue import enqueue_ugc_render_job

        with patch.dict(os.environ, {"REDIS_URL": ""}, clear=False):
            self.assertIsNone(enqueue_ugc_render_job(1))


class PlatformPresetTests(unittest.TestCase):
    def test_known_and_default(self) -> None:
        self.assertEqual(get_preset("tiktok")["key"], "tiktok")
        self.assertEqual(get_preset("reels")["key"], "reels")
        self.assertEqual(get_preset("nope")["key"], "tiktok")  # default

    def test_list_and_max_length(self) -> None:
        self.assertGreaterEqual(len(list_presets()), 4)
        self.assertEqual(max_length_for("shorts"), 60)
        self.assertEqual(max_length_for("reels"), 90)


class LearnerTests(unittest.TestCase):
    def test_aggregate_derives_ctr_cvr(self) -> None:
        rows = [
            SimpleNamespace(spend=50, impressions=1000, clicks=100, conversions=10, roas=None),
            SimpleNamespace(spend=50, impressions=1000, clicks=100, conversions=10, roas=2.0),
        ]
        agg = ugc_learner.aggregate_performance(rows)
        self.assertEqual(agg["impressions"], 2000)
        self.assertEqual(agg["clicks"], 200)
        self.assertAlmostEqual(agg["ctr"], 0.1)
        self.assertAlmostEqual(agg["cvr"], 0.1)
        self.assertAlmostEqual(agg["roas"], 2.0)

    def test_primary_metric_priority(self) -> None:
        self.assertEqual(ugc_learner.primary_metric([{"roas": 2.0, "ctr": 0.1}]), "roas")
        self.assertEqual(ugc_learner.primary_metric([{"roas": None, "cvr": 0.2}]), "cvr")
        self.assertEqual(ugc_learner.primary_metric([{"impressions": 5}]), "impressions")

    def test_recommended_dimensions(self) -> None:
        top = [
            {"provider_avatar_id": "a1", "avatar_name": "A1", "length_sec": 30, "aspect_ratio": "9:16", "hook": "h1"},
            {"provider_avatar_id": "a1", "avatar_name": "A1", "length_sec": 15, "aspect_ratio": "9:16", "hook": "h2"},
        ]
        dims = ugc_learner.recommended_dimensions(top)
        self.assertEqual([a["provider_avatar_id"] for a in dims["avatars"]], ["a1"])  # deduped
        self.assertEqual(sorted(dims["lengths"]), [15, 30])
        self.assertEqual(dims["aspect_ratios"], ["9:16"])
        self.assertEqual(dims["hooks"], ["h1", "h2"])

    def test_attribute_summary_counts(self) -> None:
        top = [
            {"angle": "testimonial", "length_sec": 30, "aspect_ratio": "9:16", "gender": "female"},
            {"angle": "testimonial", "length_sec": 15, "aspect_ratio": "9:16", "gender": "female"},
        ]
        attr = ugc_learner.attribute_summary(top)
        self.assertEqual(attr["angle"]["testimonial"], 2)
        self.assertEqual(attr["gender"]["female"], 2)
        self.assertEqual(attr["aspect"]["9:16"], 2)


if __name__ == "__main__":
    unittest.main()
