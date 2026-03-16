# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from lazr.lifecycle.interfaces import IObjectCreatedEvent
from zope.component import getUtility

from lp.answers.interfaces.question import IQuestion
from lp.registry.interfaces.person import IPerson
from lp.services.statsd.interfaces.statsd_client import IStatsdClient
from lp.services.webapp.authorization import check_permission


def send_metrics_person_created(person: IPerson, event: IObjectCreatedEvent):
    """Send metrics when a new person (user or team) is created."""
    creation_rationale = (
        person.creation_rationale.name if person.creation_rationale else None
    )
    send_metrics(
        "person.count",
        labels={
            "is_team": person.is_team,
            "creation_rationale": creation_rationale,
        },
    )


def send_metrics_question_created(
    question: IQuestion, event: IObjectCreatedEvent
):
    """Create metrics to aggregate number of questions asked."""
    is_legitimate = check_permission(
        "launchpad.AnyLegitimatePerson", question.owner
    )
    send_metrics(
        "question.count",
        labels={
            "is_legitimate": is_legitimate,
        },
    )


def send_metrics(event_name: str, labels: dict):
    """Helper function to send metrics to statsd."""
    statsd_client = getUtility(IStatsdClient)
    statsd_client.incr(
        event_name,
        labels=labels,
    )
