import unittest

from fastapi import HTTPException

from app.main import requested_roles
from app.models import User, UserRole


class MultiRoleTest(unittest.TestCase):
    def user(self):
        return User(
            email="doctor@example.com",
            display_name="测试医生",
            password_hash="unused",
            role=UserRole.ANNOTATOR,
        )

    def test_legacy_role_is_available_before_backfill(self):
        user = self.user()
        self.assertTrue(user.has_role(UserRole.ANNOTATOR))
        self.assertFalse(user.has_role(UserRole.REVIEWER))

    def test_user_can_hold_both_clinical_roles(self):
        user = self.user()
        user.set_roles({UserRole.ANNOTATOR, UserRole.REVIEWER})
        self.assertTrue(user.has_role(UserRole.ANNOTATOR))
        self.assertTrue(user.has_role(UserRole.REVIEWER))

    def test_admin_cannot_be_combined_with_clinical_roles(self):
        with self.assertRaises(HTTPException) as raised:
            requested_roles(None, [UserRole.ADMIN, UserRole.REVIEWER], allow_admin=True)
        self.assertEqual(raised.exception.status_code, 422)

    def test_admin_cannot_be_granted_through_role_editor(self):
        with self.assertRaises(HTTPException) as raised:
            requested_roles(None, [UserRole.ADMIN], allow_admin=False)
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
