import datetime
import decimal
import json
import os
import unittest

from cryptography.hazmat.primitives.ciphers.algorithms import AES, Blowfish
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.db import connection
from django.db.utils import IntegrityError
from django.test import TestCase


from pgcrypto import __version__, armor, dearmor, pad, unpad
from pgcrypto.fields import BaseEncryptedField

from .models import Employee


class CryptoTests(unittest.TestCase):
    def setUp(self):
        # This is the expected Blowfish-encrypted value, according to the following pgcrypto call:
        #     select encrypt('sensitive information', 'pass', 'bf');
        self.encrypt_bf = b"x\364r\225\356WH\347\240\205\211a\223I{~\233\034\347\217/f\035\005"
        # The basic "encrypt" call assumes an all-NUL IV of the appropriate block size.
        self.iv_blowfish = b"\0" * Blowfish.block_size
        # This is the expected AES-encrypted value, according to the following pgcrypto call:
        #     select encrypt('sensitive information', 'pass', 'aes');
        self.encrypt_aes = b"\263r\011\033]Q1\220\340\247\317Y,\321q\224KmuHf>Z\011M\032\316\376&z\330\344"
        # The basic "encrypt" call assumes an all-NUL IV of the appropriate block size.
        self.iv_aes = b"\0" * AES.block_size
        # When encrypting a string whose length is a multiple of the block size, pgcrypto
        # tacks on an extra block of padding, so it can reliably unpad afterwards. This
        # data was generated from the following query (string length = 16):
        #     select encrypt('xxxxxxxxxxxxxxxx', 'secret', 'aes');
        self.encrypt_aes_padded = (
            b"5M\304\316\240B$Z\351\021PD\317\213\213\234f\225L \342\004SIX\030\331S\376\371\220\\"
        )

    def test_encrypt(self):
        f = BaseEncryptedField(cipher="bf", key=b"pass")
        self.assertEqual(f.encrypt(pad(b"sensitive information", f.block_size)), self.encrypt_bf)

    def test_decrypt(self):
        f = BaseEncryptedField(cipher="bf", key=b"pass")
        self.assertEqual(unpad(f.decrypt(self.encrypt_bf), f.block_size), b"sensitive information")

    def test_armor_dearmor(self):
        a = armor(self.encrypt_bf)
        self.assertEqual(dearmor(a), self.encrypt_bf)

    def test_aes(self):
        f = BaseEncryptedField(cipher="aes", key=b"pass")
        self.assertEqual(f.encrypt(pad(b"sensitive information", f.block_size)), self.encrypt_aes)

    def test_aes_pad(self):
        f = BaseEncryptedField(cipher="aes", key=b"secret")
        self.assertEqual(unpad(f.decrypt(self.encrypt_aes_padded), f.block_size), b"xxxxxxxxxxxxxxxx")


class FieldTests(TestCase):
    fixtures = ("employees",)

    def setUp(self):
        # Normally, you would use django.contrib.postgres.operations.CryptoExtension in migrations.
        c = connections["default"].cursor()
        c.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    def test_query(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "employees.json")
        for obj in json.load(open(fixture_path, "r")):
            if obj["model"] == "core.employee":
                e = Employee.objects.get(ssn=obj["fields"]["ssn"])
                self.assertEqual(e.pk, int(obj["pk"]))
                self.assertEqual(e.age, 42)
                self.assertEqual(e.salary, decimal.Decimal(obj["fields"]["salary"]))
                self.assertEqual(e.date_hired.isoformat(), obj["fields"]["date_hired"])

    def test_decimal_lookups(self):
        self.assertEqual(Employee.objects.filter(salary=decimal.Decimal("75248.77")).count(), 1)
        self.assertEqual(Employee.objects.filter(salary__gte=decimal.Decimal("75248.77")).count(), 1)
        self.assertEqual(Employee.objects.filter(salary__gt=decimal.Decimal("75248.77")).count(), 0)
        self.assertEqual(Employee.objects.filter(salary__gte=decimal.Decimal("70000.00")).count(), 1)
        self.assertEqual(Employee.objects.filter(salary__lte=decimal.Decimal("70000.00")).count(), 1)
        self.assertEqual(Employee.objects.filter(salary__lt=decimal.Decimal("52000")).count(), 0)

    def test_date_lookups(self):
        self.assertEqual(Employee.objects.filter(date_hired="1999-01-23").count(), 1)
        self.assertEqual(Employee.objects.filter(date_hired__gte="1999-01-01").count(), 1)
        self.assertEqual(Employee.objects.filter(date_hired__gt="1981-01-01").count(), 2)

    def test_multi_lookups(self):
        self.assertEqual(Employee.objects.filter(date_hired__gt="1981-01-01", salary__lt=60000).count(), 1)

    def test_model_validation(self):
        obj = Employee(name="Invalid User", date_hired="2000-01-01", email="invalid")
        try:
            obj.full_clean()
            self.fail("Invalid employee object passed validation")
        except ValidationError as e:
            for f in ("salary", "email"):
                self.assertIn(f, e.error_dict)

    def test_blank(self):
        obj = Employee.objects.create(name="Test User", date_hired=datetime.date.today(), email="test@example.com")
        self.assertEqual(obj.ssn, "")
        obj.refresh_from_db()
        self.assertEqual(obj.ssn, "")
        self.assertEqual(Employee.objects.filter(ssn="").count(), 1)

    def test_empty_db_string_compare_with_non_blank(self):
        obj = Employee.objects.create(name="Test User 2", date_hired=datetime.date.today(), email="test2@example.com")
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Manually overwrite DB value to empty string
                cursor.execute("UPDATE %s SET ssn = '' WHERE id = %%s;" % (obj._meta.db_table,), [obj.pk])
        obj.refresh_from_db()
        self.assertEqual(obj.ssn, "")
        self.assertNotEqual(obj.ssn, "NON_EMPTY_STRING")
        # Try performing a bulk update in Django - in SQL dearmor("") throws an error "Corrupt ascii-armor"
        Employee.objects.filter(**{"pk": obj.pk}).exclude(**{"ssn": "XYZ"}).update(**{"ssn": "XYZ"})
        obj.delete()

    def test_null_db_val_compare_with_non_blank(self):
        obj = Employee.objects.create(
            name="Test User 3", date_hired=datetime.date.today(), email="test2@example.com", ssn_nullable=None
        )
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Manually overwrite DB value to empty string
                cursor.execute("UPDATE %s SET ssn_nullable = NULL WHERE id = %%s;" % (obj._meta.db_table,), [obj.pk])
        obj.refresh_from_db()
        self.assertEqual(obj.ssn_nullable, None)
        self.assertNotEqual(obj.ssn_nullable, "NON_EMPTY_STRING")
        Employee.objects.filter(**{"pk": obj.pk}).exclude(**{"ssn_nullable": "XYZ"}).update(**{"ssn_nullable": "XYZ"})
        obj.delete()

    def test_unique(self):
        with transaction.atomic():
            try:
                Employee.objects.create(name="Duplicate", date_hired="2000-01-01", email="johnson.sally@example.com")
                self.fail("Created duplicate email (should be unique).")
            except IntegrityError:
                pass
        # Make sure we can create another record with a NULL value for a unique field.
        e = Employee.objects.create(name="NULL Email", date_hired="2000-01-01", email=None)
        e = Employee.objects.get(pk=e.pk)
        self.assertIs(e.email, None)
        self.assertEqual(Employee.objects.filter(email__isnull=True).count(), 2)

    def test_auto_now(self):
        e = Employee.objects.create(name="Joe User", ssn="12345", salary=42000)
        self.assertEqual(e.date_hired, datetime.date.today())
        self.assertEqual(e.date_modified, Employee.objects.get(pk=e.pk).date_modified)

    def test_formfields(self):
        expected = {
            "name": forms.CharField,
            "age": forms.IntegerField,
            "ssn": forms.CharField,
            "ssn_nullable": forms.CharField,
            "salary": forms.DecimalField,
            "date_hired": forms.DateField,
            "email": forms.EmailField,
            "date_modified": forms.DateTimeField,
        }
        actual = {f.name: type(f.formfield()) for f in Employee._meta.fields if not f.primary_key}
        self.assertEqual(actual, expected)

    def test_raw_versioned(self):
        e = Employee.objects.get(ssn="666-27-9811")
        version_check = "Version: django-pgcrypto %s" % __version__
        raw_ssn = e.raw.ssn
        # Check that the correct version was stored.
        self.assertIn(version_check, raw_ssn)
        # Check that SECRET_KEY was used by default.
        f = BaseEncryptedField(key=settings.SECRET_KEY)
        self.assertEqual(f.to_python(raw_ssn), e.ssn)
        # Check that trying to decrypt with a bad key is (probably) gibberish.
        with self.assertRaises(UnicodeDecodeError):
            f = BaseEncryptedField(key="badkeyisaverybadkey")
            f.to_python(raw_ssn)
