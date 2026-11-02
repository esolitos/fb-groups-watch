import unittest
import watch


class PureTests(unittest.TestCase):
    def test_excerpt(self):
        t = "Hei  alle\n\nse https://example.com/x?y=1 og www.foo.no  ok"
        self.assertEqual(watch.clean_excerpt(t), "Hei alle se og ok")
        long = watch.clean_excerpt("a" * 300)
        self.assertEqual(len(long), 140)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(watch.clean_excerpt(None), "")

    def test_dedupe_key(self):
        self.assertEqual(watch.dedupe_key("123", "A", "x"), "id:123")
        k1 = watch.dedupe_key(None, "A", "x" * 100 + "tail1")
        k2 = watch.dedupe_key(None, "A", "x" * 100 + "tail2")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, watch.dedupe_key(None, "B", "x"))
        self.assertTrue(k1.startswith("h:") and len(k1) == 66)
        self.assertEqual(watch.dedupe_key(None, "A", "hei  der\n"),
                         watch.dedupe_key(None, "A", "hei der"))

    def test_dedupe_key_ignores_time(self):
        # time text changes between runs ("2 t" -> "3 t"); the key must not
        post = {"author": "A", "text": "same post"}
        keys = {watch.dedupe_key(None, post["author"], post["text"]) for _ in ("2 t", "3 t")}
        self.assertEqual(len(keys), 1)

    def test_is_login_url(self):
        self.assertTrue(watch.is_login_url("https://www.facebook.com/login/?next=x"))
        self.assertTrue(watch.is_login_url("https://www.facebook.com/login.php"))
        self.assertTrue(watch.is_login_url("https://www.facebook.com/checkpoint/1"))
        self.assertFalse(watch.is_login_url("https://www.facebook.com/groups/loginparents/"))
        self.assertFalse(watch.is_login_url("https://www.facebook.com/groups/42/?ref=checkpoint"))

    def test_links_and_scope(self):
        self.assertEqual(watch.build_link("42", "99"),
                         "https://www.facebook.com/groups/42/posts/99/")
        self.assertEqual(watch.parse_post_href(
            "https://www.facebook.com/groups/42/permalink/99/?x=1"), ("42", "99"))
        self.assertEqual(watch.parse_post_href("https://www.facebook.com/x"), (None, None))
        g = {"42"}
        self.assertTrue(watch.is_allowed_url(
            "https://www.facebook.com/groups/42/?sorting_setting=CHRONOLOGICAL", g))
        self.assertFalse(watch.is_allowed_url("https://www.facebook.com/groups/43/", g))
        self.assertFalse(watch.is_allowed_url("https://evilfacebook.com/groups/42/", g))
        self.assertEqual(watch.feed_url("https://www.facebook.com/groups/42/?ref=x"),
                         "https://www.facebook.com/groups/42/?sorting_setting=CHRONOLOGICAL")


if __name__ == "__main__":
    unittest.main()
