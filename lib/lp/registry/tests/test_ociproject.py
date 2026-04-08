# Copyright 2019-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `OCIProject` and `OCIProjectSet`."""

import json

from testtools.matchers import ContainsDict, Equals
from testtools.testcase import ExpectedException
from zope.component import getUtility
from zope.schema.vocabulary import getVocabularyRegistry
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.bugs.interfaces.bugtargetparent import IBugTargetParent
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.ociproject import (
    OCI_PROJECT_ALLOW_CREATE,
    CannotDeleteOCIProject,
    IOCIProject,
    IOCIProjectSet,
)
from lp.registry.interfaces.ociprojectseries import IOCIProjectSeries
from lp.registry.interfaces.product import IProduct
from lp.registry.model.ociproject import OCIProject
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.webapp.interfaces import OAuthPermission
from lp.testing import (
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestOCIProject(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_implements_interface(self):
        oci_project = self.factory.makeOCIProject()
        with admin_logged_in():
            self.assertProvides(oci_project, IOCIProject)

    def test_product_pillar(self):
        product = self.factory.makeProduct(name="some-project")
        oci_project = self.factory.makeOCIProject(pillar=product)
        self.assertEqual(product, oci_project.pillar)

    def test_prevents_moving_pillar_to_invalid_type(self):
        project = self.factory.makeProduct()
        distro = self.factory.makeDistribution()

        project_oci_project = self.factory.makeOCIProject(pillar=project)
        distro_oci_project = self.factory.makeOCIProject(pillar=distro)

        with admin_logged_in():
            project_oci_project.pillar = distro
            self.assertEqual(project_oci_project.distribution, distro)
            self.assertIsNone(project_oci_project.project)

            distro_oci_project.pillar = project
            self.assertIsNone(distro_oci_project.distribution)
            self.assertEqual(distro_oci_project.project, project)

            self.assertRaises(
                ValueError, setattr, distro_oci_project, "pillar", "Invalid"
            )

    def test_newSeries(self):
        driver = self.factory.makePerson()
        distribution = self.factory.makeDistribution(driver=driver)
        registrant = self.factory.makePerson()
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        with person_logged_in(driver):
            series = oci_project.newSeries(
                "test-series", "test-summary", registrant
            )
            self.assertProvides(series, IOCIProjectSeries)

    def test_newSeries_as_oci_project_admin(self):
        admin_person = self.factory.makePerson()
        admin_team = self.factory.makeTeam(members=[admin_person])
        distribution = self.factory.makeDistribution(
            oci_project_admin=admin_team
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        registrant = self.factory.makePerson()
        with person_logged_in(admin_person):
            series = oci_project.newSeries(
                "test-series", "test-summary", registrant
            )
            self.assertProvides(series, IOCIProjectSeries)

    def test_newSeries_bad_permissions(self):
        distribution = self.factory.makeDistribution()
        registrant = self.factory.makePerson()
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        with ExpectedException(Unauthorized):
            oci_project.newSeries("test-series", "test-summary", registrant)

    def test_series(self):
        driver = self.factory.makePerson()
        distribution = self.factory.makeDistribution(driver=driver)
        first_oci_project = self.factory.makeOCIProject(pillar=distribution)
        second_oci_project = self.factory.makeOCIProject(pillar=distribution)
        with person_logged_in(driver):
            first_series = self.factory.makeOCIProjectSeries(
                oci_project=first_oci_project
            )
            self.factory.makeOCIProjectSeries(oci_project=second_oci_project)
            self.assertContentEqual([first_series], first_oci_project.series)

    def test_name(self):
        oci_project_name = self.factory.makeOCIProjectName(name="test-name")
        oci_project = self.factory.makeOCIProject(
            ociprojectname=oci_project_name
        )
        self.assertEqual("test-name", oci_project.name)

    def test_display_name(self):
        oci_project_name = self.factory.makeOCIProjectName(name="test-name")
        oci_project = self.factory.makeOCIProject(
            ociprojectname=oci_project_name
        )
        self.assertEqual(
            "OCI project test-name for %s" % oci_project.pillar.display_name,
            oci_project.display_name,
        )

    def test_destroy_fails_if_there_are_recipes(self):
        self.useFixture(
            FeatureFixture(
                {OCI_PROJECT_ALLOW_CREATE: "on", OCI_RECIPE_ALLOW_CREATE: "on"}
            )
        )
        driver = self.factory.makePerson()
        distribution = self.factory.makeDistribution(driver=driver)
        oci_project = self.factory.makeOCIProject(pillar=distribution)

        self.factory.makeOCIRecipe(oci_project=oci_project)
        with person_logged_in(driver):
            self.assertRaises(CannotDeleteOCIProject, oci_project.destroySelf)

    def test_destroy_fails_if_there_are_git_repos(self):
        driver = self.factory.makePerson()
        distribution = self.factory.makeDistribution(driver=driver)
        oci_project = self.factory.makeOCIProject(pillar=distribution)

        self.factory.makeGitRepository(target=oci_project)

        with person_logged_in(driver):
            self.assertRaises(CannotDeleteOCIProject, oci_project.destroySelf)

    def test_destroy_fails_if_there_are_bugs(self):
        driver = self.factory.makePerson()
        distribution = self.factory.makeDistribution(driver=driver)
        oci_project = self.factory.makeOCIProject(pillar=distribution)

        self.factory.makeBug(target=oci_project)

        with person_logged_in(driver):
            self.assertRaises(CannotDeleteOCIProject, oci_project.destroySelf)

    def test_destroy_fails_for_non_driver_user(self):
        driver = self.factory.makePerson()
        distribution = self.factory.makeDistribution(driver=driver)
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        with person_logged_in(self.factory.makePerson()):
            self.assertRaises(
                Unauthorized, getattr, oci_project, "destroySelf"
            )

    def test_destroy(self):
        driver = self.factory.makePerson()
        distribution = self.factory.makeDistribution(driver=driver)
        oci_project = self.factory.makeOCIProject(pillar=distribution)

        with person_logged_in(driver):
            oci_project.newSeries("name", "summary", registrant=driver)
            oci_project.destroySelf()
        self.assertEqual(
            None,
            IStore(OCIProject)
            .find(OCIProject, OCIProject.id == oci_project.id)
            .one(),
        )

    def test_bug_target_parent_product(self):
        product = self.factory.makeProduct()
        oci_project = self.factory.makeOCIProject(pillar=product)

        parent = oci_project.bug_target_parent

        # The parent should not be None.
        self.assertIsNotNone(parent)

        # The parent should provide the IProduct interface.
        self.assertTrue(IProduct.providedBy(parent))

        # The parent should be the product itself.
        self.assertEqual(product, parent)

    def test_bug_target_parent_distribution(self):
        distribution = self.factory.makeDistribution()
        oci_project = self.factory.makeOCIProject(pillar=distribution)

        parent = oci_project.bug_target_parent

        # The parent should not be None.
        self.assertIsNotNone(parent)

        # The parent should provide the IDistribution interface.
        self.assertTrue(IDistribution.providedBy(parent))

        # The parent should provide the IBugTargetParent interface.
        self.assertTrue(IBugTargetParent.providedBy(parent))

        # The parent should be the distribution itself.
        self.assertEqual(distribution, parent)


class TestOCIProjectSet(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_implements_interface(self):
        target_set = getUtility(IOCIProjectSet)
        with admin_logged_in():
            self.assertProvides(target_set, IOCIProjectSet)

    def test_new_oci_project(self):
        registrant = self.factory.makePerson()
        distribution = self.factory.makeDistribution(owner=registrant)
        oci_project_name = self.factory.makeOCIProjectName()
        target = getUtility(IOCIProjectSet).new(
            registrant, distribution, oci_project_name
        )
        with person_logged_in(registrant):
            self.assertEqual(target.registrant, registrant)
            self.assertEqual(target.distribution, distribution)
            self.assertEqual(target.pillar, distribution)
            self.assertEqual(target.ociprojectname, oci_project_name)

    def test_getByDistributionAndName(self):
        registrant = self.factory.makePerson()
        distribution = self.factory.makeDistribution(owner=registrant)
        oci_project = self.factory.makeOCIProject(
            registrant=registrant, pillar=distribution
        )

        # Make sure there's more than one to get the result from
        self.factory.makeOCIProject(pillar=self.factory.makeDistribution())

        with person_logged_in(registrant):
            fetched_result = getUtility(IOCIProjectSet).getByPillarAndName(
                distribution, oci_project.ociprojectname.name
            )
            self.assertEqual(oci_project, fetched_result)


class TestOCIProjectWebservice(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson(displayname="Test Person")
        self.other_person = self.factory.makePerson()
        self.webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        self.other_webservice = webservice_for_person(
            self.other_person,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        self.useFixture(FeatureFixture({OCI_PROJECT_ALLOW_CREATE: ""}))

    def getAbsoluteURL(self, target):
        """Get the webservice absolute URL of the given object or relative
        path."""
        if not isinstance(target, str):
            target = api_url(target)
        return self.webservice.getAbsoluteUrl(target)

    def load_from_api(self, url):
        response = self.webservice.get(url)
        self.assertEqual(200, response.status, response.body)
        return response.jsonBody()

    def assertCanCreateOCIProject(self, distro, registrant):
        with person_logged_in(self.person):
            url = api_url(distro)
        obj = {"name": "someprojectname", "description": "My OCI project"}
        resp = self.webservice.named_post(url, "newOCIProject", **obj)
        self.assertEqual(201, resp.status, resp.body)

        new_obj_url = resp.getHeader("Location")
        oci_project = self.webservice.get(new_obj_url).jsonBody()
        with person_logged_in(self.person):
            self.assertThat(
                oci_project,
                ContainsDict(
                    {
                        "registrant_link": Equals(
                            self.getAbsoluteURL(registrant)
                        ),
                        "name": Equals(obj["name"]),
                        "description": Equals(obj["description"]),
                        "distribution_link": Equals(
                            self.getAbsoluteURL(distro)
                        ),
                    }
                ),
            )

    def test_api_get_oci_project(self):
        with person_logged_in(self.person):
            person = removeSecurityProxy(self.person)
            project = removeSecurityProxy(
                self.factory.makeOCIProject(registrant=self.person)
            )
            self.factory.makeOCIProjectSeries(
                oci_project=project, registrant=self.person
            )
            url = api_url(project)

        ws_project = self.load_from_api(url)

        series_url = "{project_path}/series".format(
            project_path=self.getAbsoluteURL(project)
        )

        self.assertThat(
            ws_project,
            ContainsDict(
                dict(
                    date_created=Equals(project.date_created.isoformat()),
                    date_last_modified=Equals(
                        project.date_last_modified.isoformat()
                    ),
                    display_name=Equals(project.display_name),
                    registrant_link=Equals(self.getAbsoluteURL(person)),
                    series_collection_link=Equals(series_url),
                )
            ),
        )

    def test_api_save_oci_project(self):
        with person_logged_in(self.person):
            # Only the owner of the distribution (which is the pillar of the
            # OCIProject) is allowed to update its attributes.
            distro = self.factory.makeDistribution(owner=self.person)
            project = removeSecurityProxy(
                self.factory.makeOCIProject(
                    registrant=self.person, pillar=distro
                )
            )
            url = api_url(project)

        new_description = "Some other description"
        resp = self.webservice.patch(
            url,
            "application/json",
            json.dumps({"description": new_description}),
        )
        self.assertEqual(209, resp.status, resp.body)

        ws_project = self.load_from_api(url)
        self.assertEqual(new_description, ws_project["description"])

    def test_api_save_oci_project_prevents_updates_from_others(self):
        with admin_logged_in():
            other_person = self.factory.makePerson()
        with person_logged_in(other_person):
            # Only the owner of the distribution (which is the pillar of the
            # OCIProject) is allowed to update its attributes.
            distro = self.factory.makeDistribution(owner=other_person)
            project = removeSecurityProxy(
                self.factory.makeOCIProject(
                    registrant=other_person,
                    pillar=distro,
                    description="old description",
                )
            )
            url = api_url(project)

        new_description = "Some other description"
        resp = self.webservice.patch(
            url,
            "application/json",
            json.dumps({"description": new_description}),
        )
        self.assertEqual(401, resp.status, resp.body)

        ws_project = self.load_from_api(url)
        self.assertEqual("old description", ws_project["description"])

    def test_create_oci_project(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)

        self.assertCanCreateOCIProject(distro, self.person)

    def test_ociproject_admin_can_create(self):
        with person_logged_in(self.person):
            owner = self.factory.makePerson()
            distro = self.factory.makeDistribution(
                owner=owner, oci_project_admin=self.person
            )
        self.assertCanCreateOCIProject(distro, self.person)

    def test_team_member_of_ociproject_admin_can_create(self):
        with admin_logged_in():
            team = self.factory.makeTeam()
            team.addMember(self.person, team.teamowner)
            distro = self.factory.makeDistribution(
                owner=team.teamowner, oci_project_admin=team
            )

        self.assertCanCreateOCIProject(distro, self.person)

    def test_not_everyone_can_create_oci_project(self):
        with person_logged_in(self.person):
            owner = self.factory.makePerson()
            distro = self.factory.makeDistribution(
                owner=owner, oci_project_admin=owner
            )
            url = api_url(distro)
        obj = {"name": "someprojectname", "description": "My OCI project"}
        resp = self.webservice.named_post(url, "newOCIProject", **obj)
        self.assertEqual(401, resp.status, resp.body)

    def test_api_create_oci_project_is_enabled_by_feature_flag(self):
        self.useFixture(FeatureFixture({OCI_PROJECT_ALLOW_CREATE: "on"}))
        with admin_logged_in():
            other_user = self.factory.makePerson()
            distro = removeSecurityProxy(
                self.factory.makeDistribution(owner=other_user)
            )

        self.assertCanCreateOCIProject(distro, self.person)

    def test_delete(self):
        with admin_logged_in():
            distribution = self.factory.makeDistribution(driver=self.person)
            oci_project = self.factory.makeOCIProject(pillar=distribution)
        with person_logged_in(self.person):
            url = api_url(oci_project)
        webservice = self.webservice
        response = webservice.delete(url, api_version="devel")
        self.assertEqual(200, response.status)
        response = webservice.get(url, api_version="devel")
        self.assertEqual(404, response.status)

    def test_set_official_recipe_via_webservice(self):
        self.useFixture(
            FeatureFixture(
                {OCI_PROJECT_ALLOW_CREATE: "on", OCI_RECIPE_ALLOW_CREATE: "on"}
            )
        )
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            oci_project = self.factory.makeOCIProject(pillar=distro)
            oci_recipe = self.factory.makeOCIRecipe(oci_project=oci_project)
            oci_recipe_url = api_url(oci_recipe)
            url = api_url(oci_project)

        obj = {"recipe": oci_recipe_url, "status": True}
        self.webservice.named_post(url, "setOfficialRecipeStatus", **obj)

        with person_logged_in(self.person):
            self.assertEqual(
                [oci_recipe], list(oci_project.getOfficialRecipes())
            )

    def test_set_official_recipe_via_webservice_incorrect_recipe(self):
        self.useFixture(
            FeatureFixture(
                {OCI_PROJECT_ALLOW_CREATE: "on", OCI_RECIPE_ALLOW_CREATE: "on"}
            )
        )
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            oci_project = self.factory.makeOCIProject(pillar=distro)
            other_project = self.factory.makeOCIProject()
            oci_recipe = self.factory.makeOCIRecipe(oci_project=other_project)
            oci_recipe_url = api_url(oci_recipe)
            url = api_url(oci_project)

        obj = {"recipe": oci_recipe_url, "status": True}
        resp = self.webservice.named_post(
            url, "setOfficialRecipeStatus", **obj
        )

        self.assertEqual(401, resp.status)
        self.assertEqual(
            b"The given recipe is invalid for this OCI project.", resp.body
        )

    def test_set_official_recipe_via_webservice_not_owner(self):
        self.useFixture(
            FeatureFixture(
                {OCI_PROJECT_ALLOW_CREATE: "on", OCI_RECIPE_ALLOW_CREATE: "on"}
            )
        )
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            oci_project = self.factory.makeOCIProject(pillar=distro)
            other_project = self.factory.makeOCIProject()
            oci_recipe = self.factory.makeOCIRecipe(oci_project=other_project)
            oci_recipe_url = api_url(oci_recipe)
            url = api_url(oci_project)

        obj = {"recipe": oci_recipe_url, "status": True}
        resp = self.other_webservice.named_post(
            url, "setOfficialRecipeStatus", **obj
        )

        self.assertEqual(401, resp.status)
        self.assertIn(b"launchpad.Edit", resp.body)


class TestOCIProjectVocabulary(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def createOCIProjects(self, name_tpl="my-ociproject-%s", count=5):
        return [
            self.factory.makeOCIProject(ociprojectname=name_tpl % i)
            for i in range(count)
        ]

    def getVocabulary(self, context=None):
        vocabulary_registry = getVocabularyRegistry()
        return vocabulary_registry.get(context, "OCIProject")

    def assertContainsSameOCIProjects(self, ociprojects, search_result):
        """Asserts that the search result contains only the given list of OCI
        projects.
        """
        naked = removeSecurityProxy
        self.assertEqual(
            {naked(ociproject).id for ociproject in ociprojects},
            {naked(term.value).id for term in search_result},
        )

    def test_search_with_name_substring(self):
        vocabulary = self.getVocabulary()
        projects = self.createOCIProjects("test-project-%s", 10)
        self.createOCIProjects("another-pattern-%s", 10)

        search_result = vocabulary.searchForTerms("test-project")
        self.assertContainsSameOCIProjects(projects, search_result)

    def test_search_without_name_substring(self):
        vocabulary = self.getVocabulary()
        projects = self.createOCIProjects()
        search_result = vocabulary.searchForTerms("")
        self.assertContainsSameOCIProjects(projects, search_result)

    def test_to_term(self):
        vocabulary = removeSecurityProxy(self.getVocabulary())
        ociproject = self.factory.makeOCIProject()
        term = removeSecurityProxy(vocabulary).toTerm(ociproject)

        expected_token = ociproject.name
        expected_title = "%s (%s)" % (
            ociproject.name,
            ociproject.pillar.displayname,
        )
        self.assertEqual(expected_token, term.token)
        self.assertEqual(expected_title, term.title)

    def test_getTermByToken(self):
        ociproject = self.factory.makeOCIProject()
        vocabulary = removeSecurityProxy(self.getVocabulary())
        vocabulary.setPillar(ociproject.pillar)
        token = ociproject.name
        term = vocabulary.getTermByToken(token)
        self.assertEqual(ociproject, term.value)
