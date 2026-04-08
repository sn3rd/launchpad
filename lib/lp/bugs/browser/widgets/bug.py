# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "BugTagsFrozenSetWidget",
    "BugTagsWidget",
    "BugWidget",
    "LargeBugTagsWidget",
    "DictBugTemplatesWidget",
]

import json
import re

from zope.component import getUtility
from zope.formlib.interfaces import (
    ConversionError,
    IInputWidget,
    WidgetInputError,
)
from zope.formlib.textwidgets import IntWidget, TextAreaWidget, TextWidget
from zope.interface import implementer
from zope.schema.interfaces import ConstraintNotSatisfied

from lp.app.errors import NotFoundError
from lp.app.validators import LaunchpadValidationError
from lp.bugs.interfaces.bug import IBugSet
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.product import IProduct


class BugWidget(IntWidget):
    """A widget for displaying a field that is bound to an IBug."""

    def _toFormValue(self, value):
        """See zope.formlib.widget.SimpleInputWidget."""
        if value == self.context.missing_value:
            return self._missing
        else:
            return value.id

    def _toFieldValue(self, input):
        """See zope.formlib.widget.SimpleInputWidget."""
        if input == self._missing:
            return self.context.missing_value
        else:
            input = input.strip()
            # Bug ids are often prefixed with '#', but getByNameOrID
            # doesn't accept such ids.
            if input.startswith("#"):
                input = input[1:]
            try:
                return getUtility(IBugSet).getByNameOrID(input)
            except (NotFoundError, ValueError):
                raise ConversionError("Not a valid bug number or nickname.")


class BugTagsWidgetBase:
    """Base class for bug tags widgets."""

    def _tagsFromFieldValue(self, tags):
        """Package up the tags for display.

        Override this to provide custom ordering for example.

        :return: `None` if there are no tags, else an iterable of tags.
        """
        if tags is None or len(tags) == 0:
            return None
        else:
            return tags

    def _tagsToFieldValue(self, tags):
        """Package up the tags for the field.

        :param tags: `None` if the submitted data was missing, otherwise an
            iterable of tags.
        """
        if tags is None:
            return []
        else:
            return sorted(set(tags))

    def _toFormValue(self, value):
        """Convert a list of strings to a single, space separated, string."""
        tags = self._tagsFromFieldValue(value)
        if tags is None:
            return self._missing
        else:
            return " ".join(tags)

    def _toFieldValue(self, input):
        """Convert a space separated string to a list of strings."""
        input = input.strip()
        if input == self._missing:
            return self._tagsToFieldValue(None)
        else:
            return self._tagsToFieldValue(
                tag.lower()
                for tag in re.split(r"[,\s]+", input)
                if len(tag) != 0
            )

    def getInputValue(self):
        try:
            return self._getInputValue()
        except WidgetInputError as input_error:
            # The standard error message isn't useful at all. We look to
            # see if it's a ConstraintNotSatisfied error and change it
            # to a better one. For simplicity, we care only about the
            # first error.
            validation_errors = input_error.errors
            for validation_error in validation_errors.args[0]:
                if isinstance(validation_error, ConstraintNotSatisfied):
                    self._error = WidgetInputError(
                        input_error.field_name,
                        input_error.widget_title,
                        LaunchpadValidationError(
                            "'%s' isn't a valid tag name. Tags must start "
                            "with a letter or number and be lowercase. The "
                            'characters "+", "-" and "." are also allowed '
                            "after the first character."
                            % validation_error.args[0]
                        ),
                    )
                raise self._error
            else:
                raise

    def _getInputValue(self):
        raise NotImplementedError("_getInputValue must be overloaded")


class BugTagsWidget(BugTagsWidgetBase, TextWidget):
    """A widget for editing bug tags in a `List` field."""

    def __init__(self, field, value_type, request):
        # We don't use value_type.
        TextWidget.__init__(self, field, request)

    def _getInputValue(self):
        return TextWidget.getInputValue(self)

    def __call__(self):
        """Return the input with a script."""
        input_markup = super().__call__()
        script_markup = """
            <a href="/+help-bugs/tag-search.html"
               class="sprite maybe action-icon"
               target="help">Tag search help</a>
            <script type="text/javascript">
            LPJS.use('event', 'lp.bugs.tags_entry', function(Y) {
                %s
                Y.on('domready', function(e) {
                     Y.lp.bugs.tags_entry.setup_tag_complete(
                         'input[id="field.%s"][type="text"]', official_tags);
                     });
                });
            </script>
            """ % (
            self.official_tags_js,
            self.context.__name__,
        )
        return input_markup + script_markup

    @property
    def official_tags_js(self):
        """Return the JavaScript of bug tags used by the bug tag completer."""
        bug_target = self.context.context
        bug_target_parent = IProduct(bug_target, None) or IDistribution(
            bug_target, None
        )
        if bug_target_parent is not None:
            official_tags = list(bug_target_parent.official_bug_tags)
        else:
            official_tags = []
        return "var official_tags = %s;" % json.dumps(official_tags)


class BugTagsFrozenSetWidget(BugTagsWidget):
    """A widget for editing bug tags in a `FrozenSet` field."""

    def _tagsFromFieldValue(self, tags):
        """Order the tags for display.

        The field value is assumed to be unordered.
        """
        if tags is None or len(tags) == 0:
            return None
        else:
            return sorted(tags)

    def _tagsToFieldValue(self, tags):
        """Return a `frozenset` of tags."""
        if tags is None:
            return frozenset()
        else:
            return frozenset(tags)


class LargeBugTagsWidget(BugTagsWidgetBase, TextAreaWidget):
    """A large widget for editing bug tags in a `List` field."""

    def __init__(self, field, value_type, request):
        # We don't use value_type.
        TextAreaWidget.__init__(self, field, request)

    def _getInputValue(self):
        return TextAreaWidget.getInputValue(self)


@implementer(IInputWidget)
class DictBugTemplatesWidget(TextAreaWidget):
    """Widget for editing bug templates as JSON or plain text.
    This currently only modifies the 'default' template. To be
    extended to addapt when adding more templates.
    """

    template_type = "bug_templates"
    template_name = "default"

    def _toFieldValue(self, input):
        """Convert the input text to a dictionary for the schema."""
        if not input:
            return {self.template_type: {self.template_name: ""}}

        # Perform length validation
        if len(input) > 50000:
            raise ConversionError(
                "The bug template is too long. If you have lots of text to "
                "add, ask to attach a file to the bug instead."
            )

        # Add text input as default bug template
        return {self.template_type: {self.template_name: input.strip()}}

    def _toFormValue(self, value):
        """Convert a dictionary from the schema back to string for the form."""
        if not value:
            return ""
        # Return the default bug template
        return value.get(self.template_type, {}).get(self.template_name, "")
