"""Password policy and canonical public reset regressions; isolated test users."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.test import TestCase, override_settings
from django.urls import get_script_prefix, reverse, set_script_prefix
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from adminx.models import FacultyLocation
from accounts.forms import UserRegisterForm


class PasswordPolicyTests(TestCase):
    def setUp(self):
        FacultyLocation.objects.create(code="policy", name="Policy", is_active=True)
        self.email = "distinctive-account@example.test"
        self.strong = "Cobalt!Harbor-72-Telescope"
        self.weak = ("aB!7", "password", "739284619057", "distinctive-account")

    def registration(self, password, repeat=None):
        return {"email": self.email, "faculty": "policy", "password1": password,
                "password2": password if repeat is None else repeat}

    def reset_url(self, user, token=None, version=None, uid=None):
        return reverse("accounts:password_reset_confirm", kwargs={
            "uidb64": uid or urlsafe_base64_encode(force_bytes(user.pk)),
            "token": token or default_token_generator.make_token(user),
            "version": user.profile.password_reset_version if version is None else version,
        })

    def test_registration_rejects_each_configured_policy_without_creating_user(self):
        for password in self.weak:
            with self.subTest(policy=self.weak.index(password)):
                form = UserRegisterForm(self.registration(password))
                self.assertFalse(form.is_valid())
                self.assertIn("password1", form.errors)
                response = self.client.post(reverse("accounts:register"), self.registration(password))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(User.objects.filter(email=self.email).exists())

    def test_registration_match_and_pending_hashed_success(self):
        form = UserRegisterForm(self.registration(self.strong, "different"))
        self.assertFalse(form.is_valid())
        self.assertIn("password2", form.errors)
        with patch("accounts.views.send_email"):
            response = self.client.post(reverse("accounts:register"), self.registration(self.strong))
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email=self.email)
        self.assertTrue(user.check_password(self.strong))
        self.assertNotEqual(user.password, self.strong)
        self.assertEqual(user.profile.status, "pending")

    def test_reset_policy_match_and_single_use(self):
        user = User.objects.create_user(self.email, email=self.email, password="Original!Pass-991")
        original_hash = user.password
        url = self.reset_url(user)
        for password in self.weak:
            with self.subTest(policy=self.weak.index(password)):
                response = self.client.post(url, {"password1": password, "password2": password})
                self.assertRedirects(response, url, fetch_redirect_response=False)
                user.refresh_from_db()
                self.assertEqual(user.password, original_hash)
                self.assertEqual(user.profile.password_reset_version, 0)
        self.client.post(url, {"password1": self.strong, "password2": "mismatch"})
        user.refresh_from_db()
        self.assertEqual(user.password, original_hash)
        self.client.post(url, {"password1": self.strong, "password2": self.strong})
        user.refresh_from_db()
        self.assertTrue(user.check_password(self.strong))
        self.assertEqual(user.profile.password_reset_version, 1)
        replacement = "Another!Strong-Password-853"
        for invalid_url in (url, self.reset_url(user, token="invalid-token"),
                            self.reset_url(user, version=0), self.reset_url(user, uid="invalid")):
            response = self.client.post(invalid_url, {"password1": replacement, "password2": replacement})
            self.assertRedirects(response, reverse("accounts:password_reset_request"), fetch_redirect_response=False)
            user.refresh_from_db()
            self.assertTrue(user.check_password(self.strong))
            self.assertEqual(user.profile.password_reset_version, 1)

    def test_canonical_reset_get_does_not_mutate_or_send_email(self):
        user = User.objects.create_user(self.email, email=self.email, password=self.strong)
        original = (user.password, user.profile.password_reset_version)
        url = reverse("accounts:password_reset_request")
        self.assertEqual(url, "/accounts/sifremi-unuttum/")
        self.assertContains(self.client.get(reverse("accounts:login")), f'href="{url}"')
        with patch("accounts.views.send_email") as send:
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.get(self.reset_url(user)).status_code, 200)
            send.assert_not_called()
        user.refresh_from_db()
        self.assertEqual((user.password, user.profile.password_reset_version), original)

    @override_settings(FORCE_SCRIPT_NAME="/security-app", URL_PREFIX="/security-app")
    def test_reset_link_honors_script_prefix(self):
        previous = get_script_prefix()
        try:
            set_script_prefix("/security-app/")
            response = self.client.get("/accounts/login/", SCRIPT_NAME="/security-app")
            self.assertContains(response, 'href="/security-app/accounts/sifremi-unuttum/"')
            self.assertEqual(self.client.get("/accounts/sifremi-unuttum/", SCRIPT_NAME="/security-app").status_code, 200)
        finally:
            set_script_prefix(previous)
