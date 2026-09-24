from django import template
from services.access_scope import is_it_admin

register = template.Library()


@register.simple_tag(takes_context=True)
def operator_admin(context):
    return is_it_admin(context["request"].user)
