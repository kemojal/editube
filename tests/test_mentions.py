import unittest
from dataclasses import dataclass

from app.services.mentions import extract_mention_handles, resolve_mentioned_users


@dataclass
class FakeUser:
    id: int
    name: str
    email: str


class MentionParsingTests(unittest.TestCase):
    def test_extract_mentions_deduplicates_and_normalizes(self) -> None:
        text = "Hey @Alice, please sync with @alice and @bob.smith!"
        self.assertEqual(extract_mention_handles(text), ["alice", "bob.smith"])

    def test_extract_mentions_ignores_invalid_handles(self) -> None:
        text = "No mentions for me: hello@company.com and @x and @@oops"
        self.assertEqual(extract_mention_handles(text), [])

    def test_resolve_mentioned_users_matches_name_and_email_local_part(self) -> None:
        users = [
            FakeUser(id=1, name="Alice Johnson", email="alice@example.com"),
            FakeUser(id=2, name="Bob Smith", email="bob.smith@example.com"),
            FakeUser(id=3, name="Carol", email="carol@example.com"),
        ]
        resolved = resolve_mentioned_users(["alice", "bobsmith", "nobody"], users)
        self.assertEqual([u.id for u in resolved], [1, 2])

    def test_resolve_mentioned_users_skips_actor(self) -> None:
        users = [
            FakeUser(id=1, name="Alice Johnson", email="alice@example.com"),
            FakeUser(id=2, name="Bob", email="bob@example.com"),
        ]
        resolved = resolve_mentioned_users(["alice", "bob"], users, actor_user_id=1)
        self.assertEqual([u.id for u in resolved], [2])


if __name__ == "__main__":
    unittest.main()
