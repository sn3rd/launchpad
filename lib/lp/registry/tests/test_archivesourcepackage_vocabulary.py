# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ArchiveSourcePackageVocabulary."""

from zope.schema import Choice
from zope.schema.vocabulary import getVocabularyRegistry

from lp.app.widgets.popup import ArchiveSourcePackagePickerWidget
from lp.registry.vocabularies import ArchiveSourcePackageVocabulary
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.soyuz.enums import PackagePublishingStatus
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class TestArchiveSourcePackageVocabulary(TestCaseWithFactory):
    """Tests for ArchiveSourcePackageVocabulary."""

    layer = DatabaseFunctionalLayer

    def _makeVocab(self, archive=None):
        vocab = ArchiveSourcePackageVocabulary()
        if archive is not None:
            vocab.setArchive(archive)
        return vocab

    def _makePublishedPackage(self, archive=None):
        if archive is None:
            archive = self.factory.makeArchive()
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=archive,
            status=PackagePublishingStatus.PUBLISHED,
        )
        return archive, spph.sourcepackagename

    # --- __init__ context auto-scoping ---

    def test_init_with_archive_context_sets_archive(self):
        # When instantiated with an IArchive context the vocabulary is
        # automatically scoped to that archive without calling setArchive.
        archive = self.factory.makeArchive()
        vocab = ArchiveSourcePackageVocabulary(context=archive)
        self.assertEqual(archive, vocab.archive)

    def test_init_with_none_context_leaves_archive_none(self):
        # None context must not crash; archive stays None.
        vocab = ArchiveSourcePackageVocabulary(context=None)
        self.assertIsNone(vocab.archive)

    def test_init_with_non_archive_context_leaves_archive_none(self):
        # A non-IArchive context must not crash; archive stays None.
        person = self.factory.makePerson()
        vocab = ArchiveSourcePackageVocabulary(context=person)
        self.assertIsNone(vocab.archive)

    # --- setArchive ---

    def test_setArchive_scopes_vocabulary(self):
        # setArchive replaces whatever archive was set previously.
        archive1 = self.factory.makeArchive()
        archive2 = self.factory.makeArchive()
        vocab = self._makeVocab(archive1)
        vocab.setArchive(archive2)
        self.assertEqual(archive2, vocab.archive)

    # --- _clauses (defensive check) ---

    def test_clauses_returns_false_when_no_archive(self):
        # The _clauses property must return [False] when archive is None
        # to prevent Storm from returning incorrect results.
        vocab = self._makeVocab()
        self.assertEqual([False], vocab._clauses)

    def test_clauses_false_makes_storm_query_return_empty(self):
        # Verify that Storm queries with [False] clause return no results.
        # This tests that the defensive pattern actually works.
        from lp.registry.model.sourcepackagename import SourcePackageName
        from lp.services.database.interfaces import IStore

        # Create a real published package to ensure DB has data
        self._makePublishedPackage()

        vocab = self._makeVocab()  # No archive set
        # Directly test what the base class does with _clauses
        results = IStore(SourcePackageName).find(
            SourcePackageName, *vocab._clauses
        )
        self.assertEqual(0, results.count())
        self.assertEqual([], list(results))

    # --- __iter__ and __len__ ---

    def test_iter_no_archive_is_empty(self):
        # Without an archive, iterating yields nothing (does not raise).
        vocab = self._makeVocab()
        self.assertEqual([], list(vocab))

    def test_len_no_archive_is_zero(self):
        # Without an archive, len() returns 0 (does not raise).
        vocab = self._makeVocab()
        self.assertEqual(0, len(vocab))

    def test_iter_with_archive_yields_published_package(self):
        # __iter__ yields one term per actively published package.
        archive, spn = self._makePublishedPackage()
        vocab = self._makeVocab(archive)
        tokens = [t.token for t in vocab]
        self.assertIn(spn.name, tokens)

    def test_iter_excludes_superseded(self):
        # Superseded publications are not yielded.
        archive = self.factory.makeArchive()
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=archive,
            status=PackagePublishingStatus.SUPERSEDED,
        )
        vocab = self._makeVocab(archive)
        tokens = [t.token for t in vocab]
        self.assertNotIn(spph.sourcepackagename.name, tokens)

    # --- toTerm ---

    def test_toTerm_returns_simple_term_with_name(self):
        # toTerm uses the source package name as both token and title.
        archive, spn = self._makePublishedPackage()
        vocab = self._makeVocab(archive)
        term = vocab.toTerm(spn)
        self.assertEqual(spn, term.value)
        self.assertEqual(spn.name, term.token)
        self.assertEqual(spn.name, term.title)

    # --- getTermByToken ---

    def test_getTermByToken_no_archive_raises(self):
        # With no archive set, any lookup raises LookupError.
        vocab = self._makeVocab()
        self.assertRaises(LookupError, vocab.getTermByToken, "python")

    def test_getTermByToken_found(self):
        # A published package is found by its name.
        archive, spn = self._makePublishedPackage()
        vocab = self._makeVocab(archive)
        term = vocab.getTermByToken(spn.name)
        self.assertEqual(spn, term.value)

    def test_getTermByToken_not_in_archive_raises(self):
        # A package not published in the archive raises LookupError.
        archive = self.factory.makeArchive()
        vocab = self._makeVocab(archive)
        self.assertRaises(LookupError, vocab.getTermByToken, "no-such-pkg")

    def test_getTermByToken_superseded_raises(self):
        # A superseded publication is not found.
        archive = self.factory.makeArchive()
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=archive,
            status=PackagePublishingStatus.SUPERSEDED,
        )
        vocab = self._makeVocab(archive)
        self.assertRaises(
            LookupError, vocab.getTermByToken, spph.sourcepackagename.name
        )

    # --- search ---

    def test_search_no_archive_returns_empty(self):
        # Without an archive, search always returns nothing.
        vocab = self._makeVocab()
        results = list(vocab.search("python"))
        self.assertEqual([], results)

    def test_search_empty_query_returns_empty(self):
        # An empty query string returns nothing even with an archive set.
        archive = self.factory.makeArchive()
        vocab = self._makeVocab(archive)
        results = list(vocab.search(""))
        self.assertEqual([], results)

    def test_search_matches_published_package(self):
        # A substring of a published package name returns that package.
        archive, spn = self._makePublishedPackage()
        vocab = self._makeVocab(archive)
        results = list(vocab.search(spn.name))
        self.assertIn(spn, results)

    def test_search_no_match_returns_empty(self):
        # A query with no matching packages returns nothing.
        archive = self.factory.makeArchive()
        vocab = self._makeVocab(archive)
        results = list(vocab.search("zzz-no-such-package"))
        self.assertEqual([], results)

    def test_search_excludes_superseded(self):
        # Superseded publications are excluded from search results.
        archive = self.factory.makeArchive()
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=archive,
            status=PackagePublishingStatus.SUPERSEDED,
        )
        vocab = self._makeVocab(archive)
        results = list(vocab.search(spph.sourcepackagename.name))
        self.assertNotIn(spph.sourcepackagename, results)

    def test_search_scoped_to_archive(self):
        # Results from search are limited to the scoped archive.
        archive1, spn1 = self._makePublishedPackage()
        archive2, spn2 = self._makePublishedPackage()
        vocab = self._makeVocab(archive1)
        results = list(vocab.search(spn2.name))
        self.assertNotIn(spn2, results)

    # --- searchForTerms (used by AJAX vocabulary picker) ---

    def test_searchForTerms_no_archive_returns_empty(self):
        # With no archive set, searchForTerms returns 0 results.
        vocab = self._makeVocab()
        self.assertEqual(0, vocab.searchForTerms("python").count())

    def test_searchForTerms_empty_query_returns_empty(self):
        # An empty query returns nothing even with an archive set.
        archive = self.factory.makeArchive()
        vocab = self._makeVocab(archive)
        self.assertEqual(0, vocab.searchForTerms("").count())

    def test_searchForTerms_with_match(self):
        # A query matching a published package returns one term.
        archive, spn = self._makePublishedPackage()
        vocab = self._makeVocab(archive)
        results = vocab.searchForTerms(spn.name)
        self.assertEqual(1, results.count())
        terms = list(results)
        self.assertEqual(spn.name, terms[0].token)

    def test_searchForTerms_archive_context_finds_package(self):
        # The AJAX picker path: factory(archive) then searchForTerms works.
        archive, spn = self._makePublishedPackage()
        vocab = ArchiveSourcePackageVocabulary(context=archive)
        results = vocab.searchForTerms(spn.name)
        self.assertEqual(1, results.count())


class TestArchiveSourcePackagePickerWidget(TestCaseWithFactory):
    """Tests for the picker widget that renders the archive context JS."""

    layer = DatabaseFunctionalLayer

    def _makeWidget(self, prefix="field.target"):
        archive = self.factory.makeArchive()
        vocabulary_registry = getVocabularyRegistry()
        vocab = vocabulary_registry.get(archive, "ArchiveSourcePackageName")
        field = Choice(
            __name__="ppa_package", vocabulary="ArchiveSourcePackageName"
        )
        bound_field = field.bind(archive)
        request = LaunchpadTestRequest()
        widget = ArchiveSourcePackagePickerWidget(bound_field, vocab, request)
        widget.setPrefix(prefix)
        return widget

    def test_ppa_id_reflects_prefix(self):
        """ppa_id must be <prefix>.ppa – the form input the JS reads."""
        widget = self._makeWidget(prefix="field.target")
        self.assertEqual("field.target.ppa", widget.ppa_id)

    def test_rendered_markup_contains_ppa_id(self):
        """The rendered template must embed the ppa_id for the JS picker."""
        widget = self._makeWidget(prefix="field.target")
        markup = widget()
        self.assertIn("ppa_id = 'field.target.ppa'", markup)
