# Copyright 2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test the bugtask widgets."""

from zope.formlib.interfaces import ConversionError
from zope.schema import Choice
from zope.schema.vocabulary import getVocabularyRegistry
from zope.security.proxy import removeSecurityProxy

from lp.bugs.browser.widgets.bugtask import (
    BugTaskTargetWidget,
    FileBugArchiveSourcePackageNameWidget,
)
from lp.bugs.interfaces.bugtask import IBugTask
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.testing import TestCaseWithFactory, login_person
from lp.testing.layers import DatabaseFunctionalLayer


class BugTaskTargetWidgetTestCase(TestCaseWithFactory):
    """Test that BugTaskTargetWidget behaves as expected."""

    layer = DatabaseFunctionalLayer

    def getWidget(self, bugtask):
        field = IBugTask["target"]
        bound_field = field.bind(bugtask)
        request = LaunchpadTestRequest()
        return BugTaskTargetWidget(bound_field, request)

    def test_getDistributionVocabulary_with_product_bugtask(self):
        # The vocabulary does not contain distros that do not use
        # launchpad to track bugs.
        distribution = self.factory.makeDistribution()
        product = self.factory.makeProduct()
        bugtask = self.factory.makeBugTask(target=product)
        target_widget = self.getWidget(bugtask)
        vocabulary = target_widget.getDistributionVocabulary()
        self.assertEqual(None, vocabulary.distribution)
        self.assertFalse(
            distribution in vocabulary,
            "Vocabulary contains distros that do not use Launchpad Bugs.",
        )

    def test_getDistributionVocabulary_with_distribution_bugtask(self):
        # The vocabulary does not contain distros that do not use
        # launchpad to track bugs.
        distribution = self.factory.makeDistribution()
        other_distribution = self.factory.makeDistribution()
        bugtask = self.factory.makeBugTask(target=distribution)
        target_widget = self.getWidget(bugtask)
        vocabulary = target_widget.getDistributionVocabulary()
        self.assertEqual(distribution, vocabulary.distribution)
        self.assertTrue(
            distribution in vocabulary,
            "Vocabulary missing context distribution.",
        )
        self.assertFalse(
            other_distribution in vocabulary,
            "Vocabulary contains distros that do not use Launchpad Bugs.",
        )


class TestFileBugArchiveSourcePackageNameWidget(TestCaseWithFactory):
    """Tests for FileBugArchiveSourcePackageNameWidget."""

    layer = DatabaseFunctionalLayer

    def _make_widget(self, context):
        """Create a FileBugArchiveSourcePackageNameWidget bound to context."""
        vocabulary_registry = getVocabularyRegistry()
        vocabulary = vocabulary_registry.get(
            context, "ArchiveSourcePackageName"
        )
        field = Choice(
            __name__="packagename", vocabulary="ArchiveSourcePackageName"
        )
        bound_field = field.bind(context)
        bound_field.vocabulary = vocabulary
        return FileBugArchiveSourcePackageNameWidget(
            bound_field, vocabulary, LaunchpadTestRequest()
        )

    def test_call_descriptor_does_not_raise_on_archive_context(self):
        # __call__ uses VocabularyPickerWidget.__dict__["__call__"] so that
        # it gets the raw ViewPageTemplateFile descriptor rather than a
        # BoundPageTemplate(tpl, None) that would raise IndexError.
        ppa = self.factory.makeArchive()
        login_person(ppa.owner)
        widget = self._make_widget(ppa)
        # Calling the widget should not raise an IndexError.
        html = widget()
        self.assertIsNotNone(html)

    def test_init_scopes_vocabulary_to_archive(self):
        # __init__ calls vocabulary.setArchive so the vocabulary is
        # immediately scoped to the archive context.
        ppa = self.factory.makeArchive()
        login_person(ppa.owner)
        widget = self._make_widget(ppa)
        self.assertEqual(
            ppa, removeSecurityProxy(widget.context.vocabulary).archive
        )

    def test_init_scopes_vocabulary_to_archive_from_asp(self):
        # __init__ extracts the archive from an IArchiveSourcePackage context.
        ppa = self.factory.makeArchive()
        spn = self.factory.makeSourcePackageName()
        login_person(ppa.owner)
        self.factory.makeSourcePackagePublishingHistory(
            archive=ppa, sourcepackagename=spn
        )
        asp = ppa.getArchiveSourcePackage(spn)
        widget = self._make_widget(asp)
        self.assertEqual(
            ppa, removeSecurityProxy(widget.context.vocabulary).archive
        )

    def test_toFieldValue_returns_missing_for_empty_input(self):
        # _toFieldValue returns the field's missing value for an empty string.
        ppa = self.factory.makeArchive()
        login_person(ppa.owner)
        widget = self._make_widget(ppa)
        result = widget._toFieldValue("")
        self.assertIs(widget.context.missing_value, result)

    def test_toFieldValue_resolves_known_package(self):
        # _toFieldValue returns the SourcePackageName for a published package.
        ppa = self.factory.makeArchive()
        spn = self.factory.makeSourcePackageName()
        login_person(ppa.owner)
        self.factory.makeSourcePackagePublishingHistory(
            archive=ppa, sourcepackagename=spn
        )
        widget = self._make_widget(ppa)
        result = widget._toFieldValue(spn.name)
        self.assertEqual(spn, result)

    def test_toFieldValue_raises_for_unknown_package(self):
        # _toFieldValue raises ConversionError for a package not in the
        # archive.
        ppa = self.factory.makeArchive()
        login_person(ppa.owner)
        widget = self._make_widget(ppa)
        self.assertRaises(
            ConversionError, widget._toFieldValue, "no-such-package"
        )
