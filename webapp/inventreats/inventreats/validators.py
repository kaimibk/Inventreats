"""
Custom field validators for InvenTree
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from moneyed import CURRENCIES

import common.models

import re

def validate_tree_name(value):
    """ Prevent illegal characters in tree item names """

    for c in "!@#$%^&*'\"\\/[]{}<>,|+=~`\"":
        if c in str(value):
            raise ValidationError(_('Illegal character in name ({x})'.format(x=c)))
