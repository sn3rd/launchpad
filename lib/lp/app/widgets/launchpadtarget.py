# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from zope.browserpage import ViewPageTemplateFile
from zope.component import getUtility
from zope.formlib.interfaces import (
    ConversionError,
    IInputWidget,
    InputErrors,
    MissingInputError,
    WidgetInputError,
)
from zope.formlib.utility import setUpWidget
from zope.formlib.widget import (
    BrowserWidget,
    CustomWidgetFactory,
    InputWidget,
    renderElement,
)
from zope.interface import implementer
from zope.schema import Choice

from lp.app.errors import NotFoundError, UnexpectedFormData
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.validators import LaunchpadValidationError
from lp.app.widgets.itemswidgets import LaunchpadDropdownWidget
from lp.registry.interfaces.archivesourcepackage import IArchiveSourcePackage
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.externalpackage import IExternalPackage
from lp.registry.interfaces.product import IProduct
from lp.services.features import getFeatureFlag
from lp.services.webapp.interfaces import (
    IAlwaysSubmittedWidget,
    IMultiLineWidgetLayout,
)
from lp.soyuz.interfaces.archive import ARCHIVE_BUGS_FEATURE_FLAG, IArchive


@implementer(IAlwaysSubmittedWidget, IMultiLineWidgetLayout, IInputWidget)
class LaunchpadTargetWidget(BrowserWidget, InputWidget):
    """Widget for selecting a product, distribution or package target."""

    template = ViewPageTemplateFile("templates/launchpad-target.pt")
    default_option = "package"
    _widgets_set_up = False

    def getDistributionVocabulary(self):
        return "Distribution"

    def getProductVocabulary(self):
        return "Product"

    def getPackageVocabularyName(self):
        if bool(getFeatureFlag("disclosure.dsp_picker.enabled")):
            # Replace the default field with a field that uses the better
            # vocabulary.
            return "DistributionSourcePackage"
        else:
            return "BinaryAndSourcePackageName"

    @property
    def show_ppa_option(self):
        """Whether to show the PPA option."""
        return bool(getFeatureFlag(ARCHIVE_BUGS_FEATURE_FLAG))

    def setUpSubWidgets(self):
        if self._widgets_set_up:
            return
        fields = [
            Choice(
                __name__="product",
                title="Project",
                required=True,
                vocabulary=self.getProductVocabulary(),
            ),
            Choice(
                __name__="distribution",
                title="Distribution",
                required=True,
                vocabulary=self.getDistributionVocabulary(),
                default=getUtility(ILaunchpadCelebrities).ubuntu,
            ),
            Choice(
                __name__="package",
                title="Package",
                required=False,
                vocabulary=self.getPackageVocabularyName(),
            ),
        ]
        if self.show_ppa_option:
            fields.extend(
                [
                    Choice(
                        __name__="ppa",
                        title="PPA",
                        required=True,
                        vocabulary="PPA",
                    ),
                    Choice(
                        __name__="ppa_package",
                        title="Package",
                        required=False,
                        vocabulary="ArchiveSourcePackageName",
                    ),
                ]
            )
        self.distribution_widget = CustomWidgetFactory(LaunchpadDropdownWidget)
        for field in fields:
            setUpWidget(
                self, field.__name__, field, IInputWidget, prefix=self.name
            )
        self._widgets_set_up = True

    def setUpOptions(self):
        """Set up options to be rendered."""
        self.options = {}
        option_list = ["package", "product"]
        if self.show_ppa_option:
            option_list.append("ppa")
        for option in option_list:
            attributes = dict(
                type="radio",
                name=self.name,
                value=option,
                id="%s.option.%s" % (self.name, option),
            )
            if (
                self.request.form_ng.getOne(self.name, self.default_option)
                == option
            ):
                attributes["checked"] = "checked"
            self.options[option] = renderElement("input", **attributes)
        self.package_widget.onKeyPress = (
            "selectWidget('%s.option.package', event)" % self.name
        )
        self.product_widget.onKeyPress = (
            "selectWidget('%s.option.product', event)" % self.name
        )
        if self.show_ppa_option:
            self.ppa_widget.onKeyPress = (
                "selectWidget('%s.option.ppa', event)" % self.name
            )
            self.ppa_package_widget.onKeyPress = (
                "selectWidget('%s.option.ppa', event)" % self.name
            )

    def hasInput(self):
        return self.name in self.request.form

    def hasValidInput(self):
        """See zope.formlib.interfaces.IInputWidget."""
        try:
            self.getInputValue()
            return True
        except InputErrors:
            return False
        except UnexpectedFormData:
            return False

    def getInputValue(self):
        """See zope.formlib.interfaces.IInputWidget."""
        self.setUpSubWidgets()
        form_value = self.request.form_ng.getOne(self.name)
        if form_value == "product":
            try:
                return self.product_widget.getInputValue()
            except MissingInputError:
                self._error = WidgetInputError(
                    self.name,
                    self.label,
                    LaunchpadValidationError("Please enter a project name"),
                )
                raise self._error
            except ConversionError:
                entered_name = self.request.form_ng.getOne(
                    "%s.product" % self.name
                )
                self._error = WidgetInputError(
                    self.name,
                    self.label,
                    LaunchpadValidationError(
                        "There is no project named '%s' registered in"
                        " Launchpad" % entered_name
                    ),
                )
                raise self._error
        elif form_value == "package":
            try:
                distribution = self.distribution_widget.getInputValue()
            except ConversionError:
                entered_name = self.request.form_ng.getOne(
                    "%s.distribution" % self.name
                )
                self._error = WidgetInputError(
                    self.name,
                    self.label,
                    LaunchpadValidationError(
                        "There is no distribution named '%s' registered in"
                        " Launchpad" % entered_name
                    ),
                )
                raise self._error
            if self.package_widget.hasInput():
                if bool(getFeatureFlag("disclosure.dsp_picker.enabled")):
                    self.package_widget.vocabulary.setDistribution(
                        distribution
                    )
                try:
                    package_name = self.package_widget.getInputValue()
                    if package_name is None:
                        return distribution
                    if IDistributionSourcePackage.providedBy(package_name):
                        dsp = package_name
                    else:
                        source_name = (
                            distribution.guessPublishedSourcePackageName(
                                package_name.name
                            )
                        )
                        dsp = distribution.getSourcePackage(source_name)
                except (ConversionError, NotFoundError):
                    entered_name = self.request.form_ng.getOne(
                        "%s.package" % self.name
                    )
                    self._error = WidgetInputError(
                        self.name,
                        self.label,
                        LaunchpadValidationError(
                            "There is no package named '%s' published in %s."
                            % (entered_name, distribution.displayname)
                        ),
                    )
                    raise self._error
                return dsp
            else:
                return distribution
        elif form_value == "ppa":
            try:
                archive = self.ppa_widget.getInputValue()
            except MissingInputError:
                self._error = WidgetInputError(
                    self.name,
                    self.label,
                    LaunchpadValidationError("Please select a PPA"),
                )
                raise self._error
            except ConversionError:
                entered_name = self.request.form_ng.getOne(
                    "%s.ppa" % self.name
                )
                self._error = WidgetInputError(
                    self.name,
                    self.label,
                    LaunchpadValidationError(
                        "There is no PPA with reference '%s' registered in"
                        " Launchpad" % entered_name
                    ),
                )
                raise self._error
            self.ppa_package_widget.vocabulary.setArchive(archive)
            if self.ppa_package_widget.hasInput():
                try:
                    package_name = self.ppa_package_widget.getInputValue()
                    if package_name is None:
                        return archive
                    # ArchiveSourcePackageVocabulary returns SourcePackageName
                    return archive.getArchiveSourcePackage(package_name)
                except (ConversionError, NotFoundError):
                    entered_name = self.request.form_ng.getOne(
                        "%s.ppa_package" % self.name
                    )
                    self._error = WidgetInputError(
                        self.name,
                        self.label,
                        LaunchpadValidationError(
                            "There is no package named '%s' published in %s."
                            % (entered_name, archive.displayname)
                        ),
                    )
                    raise self._error
            else:
                return archive
        else:
            raise UnexpectedFormData("No valid option was selected.")

    def setRenderedValue(self, value):
        """See IWidget."""
        self.setUpSubWidgets()
        if IProduct.providedBy(value):
            self.default_option = "product"
            self.product_widget.setRenderedValue(value)
        elif IDistribution.providedBy(value):
            self.default_option = "package"
            self.distribution_widget.setRenderedValue(value)
        elif IDistributionSourcePackage.providedBy(value):
            self.default_option = "package"
            self.distribution_widget.setRenderedValue(value.distribution)
            self.package_widget.setRenderedValue(value.sourcepackagename)
        elif IExternalPackage.providedBy(value):
            self.default_option = "package"
            self.distribution_widget.setRenderedValue(value.distribution)
            # TODO enriqueensanchz 2025-07-22: add a widget for externalpackage
            # if necessary
        elif IArchive.providedBy(value):
            self.default_option = "ppa"
            self.ppa_widget.setRenderedValue(value)
        elif IArchiveSourcePackage.providedBy(value):
            self.default_option = "ppa"
            self.ppa_widget.setRenderedValue(value.archive)
            self.ppa_package_widget.setRenderedValue(value.sourcepackagename)
        else:
            raise AssertionError("Not a valid value: %r" % value)

    def __call__(self):
        """See zope.formlib.interfaces.IBrowserWidget."""
        self.setUpSubWidgets()
        self.setUpOptions()
        return self.template()
