# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from lp.registry.interfaces.person import PersonCreationRationale
from lp.services.statsd.tests import StatsMixin
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadZopelessLayer


class TestPersonStats(TestCaseWithFactory, StatsMixin):
    """Tests that the metrics are sent to statsd when a person is created."""

    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.setUpStats()

    def test_person_count_metric(self):
        # When a person is created, a metric should be sent to statsd with the
        # "person.count" name and, label with the person's "is_team" value.
        self.factory.makePerson()

        self.assertEqual(1, self.stats_client.incr.call_count)
        self.stats_client.incr.assert_called_with(
            "person.count,creation_rationale=UNKNOWN,env=test,is_team=False"
        )

    def test_person_count_metric_team(self):
        # When a team is created, a metric should be sent to statsd, and
        # the label "is_team" should be True.

        owner = self.factory.makePerson()

        # Metric was created when creating the owner person
        self.assertEqual(1, self.stats_client.incr.call_count)
        self.stats_client.incr.assert_called_with(
            "person.count,creation_rationale=UNKNOWN,env=test,is_team=False"
        )
        self.stats_client.incr.reset_mock()

        self.factory.makeTeam(owner=owner)

        # Metric was created when creating the team
        self.assertEqual(1, self.stats_client.incr.call_count)
        self.stats_client.incr.assert_called_with(
            "person.count,creation_rationale=None,env=test,is_team=True"
        )

    def test_person_count_metric_rationale(self):
        # When a user is created due to a bug import, a metric should be sent
        # to statsd with the appropriate label "creation_rationale" value
        self.factory.makePerson(
            creation_rationale=PersonCreationRationale.BUGIMPORT
        )

        self.assertEqual(1, self.stats_client.incr.call_count)
        self.stats_client.incr.assert_called_with(
            "person.count,creation_rationale=BUGIMPORT,env=test,is_team=False"
        )


class TestQuestionStats(TestCaseWithFactory, StatsMixin):
    """Tests that metrics are sent when a question is asked."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.setUpStats()

    def test_question_count_metric_owner_legitimate(self):
        # When a legitimate user asks a question, a metric should be sent to
        # statsd with a True is_legitimate value.
        self.pushConfig(
            "launchpad", min_legitimate_karma=5, min_legitimate_account_age=5
        )
        # Karma set to 1000 to make them legitimate
        legitimate_user = self.factory.makePerson(karma=1000)
        self.factory.makeQuestion(owner=legitimate_user)
        # Check that question.count metric was called
        question_calls = self.filterStatsdCallsByName("question.count")
        # Assert that question.count was called once and that the user was
        # legitimate
        self.assertEqual(1, len(question_calls))
        self.stats_client.incr.assert_called_with(
            "question.count,env=test,is_legitimate=True"
        )

    def test_question_count_metric_owner_non_legitimate(self):
        # When a non-legitimate user asks a question, a metric should be sent
        # to statsd with a False is_legitimate value.
        self.pushConfig(
            "launchpad", min_legitimate_karma=5, min_legitimate_account_age=5
        )
        # Karma set to 0 to make them non-legitimate
        non_legitimate_user = self.factory.makePerson(karma=0)
        self.factory.makeQuestion(owner=non_legitimate_user)
        # Check that question.count metric was called
        question_calls = self.filterStatsdCallsByName("question.count")

        # Assert that question.count was called once and that the user was
        # non_legitimate
        self.assertEqual(1, len(question_calls))
        self.stats_client.incr.assert_called_with(
            "question.count,env=test,is_legitimate=False"
        )
