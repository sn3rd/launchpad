# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "ProductRelease",
    "ProductReleaseFile",
    "ProductReleaseSet",
    "productrelease_to_milestone",
]

import os
from datetime import timezone
from io import BufferedIOBase, BytesIO
from operator import itemgetter

from storm.expr import And, Desc, Join, LeftJoin
from storm.info import ClassAlias
from storm.properties import DateTime, Int, Unicode
from storm.references import Reference, ReferenceSet
from storm.store import EmptyResultSet, Store
from zope.component import getUtility
from zope.interface import implementer

from lp.app.enums import InformationType
from lp.app.errors import NotFoundError
from lp.registry.errors import InvalidFilename, ProprietaryPillar
from lp.registry.interfaces.person import (
    validate_person,
    validate_public_person,
)
from lp.registry.interfaces.productrelease import (
    IProductRelease,
    IProductReleaseFile,
    IProductReleaseSet,
    UpstreamFileType,
)
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.webapp.publisher import (
    get_raw_form_value_from_current_request,
)


@implementer(IProductRelease)
class ProductRelease(StormBase):
    """A release of a product."""

    __storm_table__ = "ProductRelease"
    __storm_order__ = ("-datereleased",)

    id = Int(primary=True)
    datereleased = DateTime(allow_none=False, tzinfo=timezone.utc)
    release_notes = Unicode(allow_none=True, default=None)
    changelog = Unicode(allow_none=True, default=None)
    datecreated = DateTime(
        name="datecreated",
        allow_none=False,
        default=UTC_NOW,
        tzinfo=timezone.utc,
    )
    owner_id = Int(name="owner", validator=validate_person, allow_none=False)
    owner = Reference(owner_id, "Person.id")
    milestone_id = Int(name="milestone", allow_none=False)
    milestone = Reference(milestone_id, "Milestone.id")

    _files = ReferenceSet(
        "id",
        "ProductReleaseFile.productrelease_id",
        order_by=Desc("ProductReleaseFile.date_uploaded"),
    )

    def __init__(
        self,
        datereleased,
        owner,
        milestone,
        release_notes=None,
        changelog=None,
    ):
        super().__init__()
        self.owner = owner
        self.milestone = milestone
        self.datereleased = datereleased
        self.release_notes = release_notes
        self.changelog = changelog

    # This is cached so that
    # lp.registry.model.product.get_precached_products can populate the
    # cache from a bulk query.
    @cachedproperty
    def files(self):
        return self._files

    @property
    def version(self):
        """See `IProductRelease`."""
        return self.milestone.name

    @property
    def productseries(self):
        """See `IProductRelease`."""
        return self.milestone.productseries

    @property
    def product(self):
        """See `IProductRelease`."""
        return self.milestone.productseries.product

    @property
    def displayname(self):
        """See `IProductRelease`."""
        return self.milestone.displayname

    @property
    def title(self):
        """See `IProductRelease`."""
        return self.milestone.title

    @property
    def can_have_release_files(self):
        """See `IProductRelease`."""
        return self.product.information_type == InformationType.PUBLIC

    @staticmethod
    def normalizeFilename(filename):
        # Replace slashes in the filename with less problematic dashes.
        return filename.replace("/", "-")

    def destroySelf(self):
        """See `IProductRelease`."""
        assert self._files.count() == 0, (
            "You can't delete a product release which has files associated "
            "with it."
        )
        # Mirror the cache invalidation done in Milestone.createProductRelease
        # so callers always see a consistent product_release cache.
        cache = get_property_cache(self.milestone)
        if "product_release" in cache:
            del cache.product_release
        Store.of(self).remove(self)

    def _getFileObjectAndSize(self, file_or_data):
        """Return an object and length for file_or_data.

        :param file_or_data: `bytes` or `io.BufferedIOBase`.
        :return: binary file object or `io.BytesIO` object and size.
        """
        if isinstance(file_or_data, bytes):
            file_size = len(file_or_data)
            file_obj = BytesIO(file_or_data)
        else:
            assert isinstance(
                file_or_data, BufferedIOBase
            ), "file_or_data is not an expected type"
            file_obj = file_or_data
            start = file_obj.tell()
            file_obj.seek(0, os.SEEK_END)
            file_size = file_obj.tell()
            file_obj.seek(start)
        return file_obj, file_size

    def addReleaseFile(
        self,
        filename,
        file_content,
        content_type,
        uploader,
        signature_filename=None,
        signature_content=None,
        file_type=UpstreamFileType.CODETARBALL,
        description=None,
        from_api=False,
    ):
        """See `IProductRelease`."""
        if not self.can_have_release_files:
            raise ProprietaryPillar(
                "Only public projects can have download files."
            )
        if self.hasReleaseFile(filename):
            raise InvalidFilename
        # Create the alias for the file.
        filename = self.normalizeFilename(filename)
        # XXX: StevenK 2013-02-06 bug=1116954: We should not need to refetch
        # the file content from the request, since the passed in one has been
        # wrongly encoded.
        if from_api:
            file_content = get_raw_form_value_from_current_request(
                file_content, "file_content"
            )
        file_obj, file_size = self._getFileObjectAndSize(file_content)

        alias = getUtility(ILibraryFileAliasSet).create(
            name=filename,
            size=file_size,
            file=file_obj,
            contentType=content_type,
        )
        if signature_filename is not None and signature_content is not None:
            # XXX: StevenK 2013-02-06 bug=1116954: We should not need to
            # refetch the file content from the request, since the passed in
            # one has been wrongly encoded.
            if from_api:
                signature_content = get_raw_form_value_from_current_request(
                    signature_content, "signature_content"
                )
            signature_obj, signature_size = self._getFileObjectAndSize(
                signature_content
            )
            signature_filename = self.normalizeFilename(signature_filename)
            signature_alias = getUtility(ILibraryFileAliasSet).create(
                name=signature_filename,
                size=signature_size,
                file=signature_obj,
                contentType="application/pgp-signature",
            )
        else:
            signature_alias = None
        return ProductReleaseFile(
            productrelease=self,
            libraryfile=alias,
            signature=signature_alias,
            filetype=file_type,
            description=description,
            uploader=uploader,
        )

    def getFileAliasByName(self, name):
        """See `IProductRelease`."""
        for file_ in self.files:
            if file_.libraryfile.filename == name:
                return file_.libraryfile
            elif file_.signature and file_.signature.filename == name:
                return file_.signature
        raise NotFoundError(name)

    def getProductReleaseFileByName(self, name):
        """See `IProductRelease`."""
        for file_ in self.files:
            if file_.libraryfile.filename == name:
                return file_
        raise NotFoundError(name)

    def hasReleaseFile(self, name):
        """See `IProductRelease`."""
        try:
            self.getProductReleaseFileByName(name)
            return True
        except NotFoundError:
            return False


@implementer(IProductReleaseFile)
class ProductReleaseFile(StormBase):
    """A file of a product release."""

    __storm_table__ = "ProductReleaseFile"

    id = Int(primary=True)

    productrelease_id = Int(name="productrelease", allow_none=False)
    productrelease = Reference(productrelease_id, "ProductRelease.id")

    libraryfile_id = Int(name="libraryfile", allow_none=False)
    libraryfile = Reference(libraryfile_id, "LibraryFileAlias.id")

    signature_id = Int(name="signature", allow_none=True)
    signature = Reference(signature_id, "LibraryFileAlias.id")

    filetype = DBEnum(
        name="filetype",
        enum=UpstreamFileType,
        allow_none=False,
        default=UpstreamFileType.CODETARBALL,
    )

    description = Unicode(name="description", allow_none=True, default=None)

    uploader_id = Int(
        name="uploader", validator=validate_public_person, allow_none=False
    )
    uploader = Reference(uploader_id, "Person.id")

    date_uploaded = DateTime(
        allow_none=False, default=UTC_NOW, tzinfo=timezone.utc
    )

    def __init__(
        self,
        productrelease,
        libraryfile,
        filetype,
        uploader,
        signature=None,
        description=None,
    ):
        super().__init__()
        self.productrelease = productrelease
        self.libraryfile = libraryfile
        self.filetype = filetype
        self.uploader = uploader
        self.signature = signature
        self.description = description

    def destroySelf(self):
        """See `IProductReleaseFile`."""
        Store.of(self).remove(self)


@implementer(IProductReleaseSet)
class ProductReleaseSet:
    """See `IProductReleaseSet`."""

    def getBySeriesAndVersion(self, productseries, version, default=None):
        """See `IProductReleaseSet`."""
        # Local import of Milestone to avoid circular imports.
        from lp.registry.model.milestone import Milestone

        store = IStore(productseries)
        # The Milestone is cached too because most uses of a ProductRelease
        # need it.
        result = store.find(
            (ProductRelease, Milestone),
            Milestone.productseries == productseries,
            ProductRelease.milestone == Milestone.id,
            Milestone.name == version,
        )
        found = result.one()
        if found is None:
            return None
        product_release, milestone = found
        return product_release

    def getReleasesForSeries(self, series):
        """See `IProductReleaseSet`."""
        # Local import of Milestone to avoid import loop.
        from lp.registry.model.milestone import Milestone

        if len(list(series)) == 0:
            return EmptyResultSet()
        series_ids = [s.id for s in series]
        return (
            IStore(ProductRelease)
            .find(
                ProductRelease,
                And(ProductRelease.milestone == Milestone.id),
                Milestone.productseries_id.is_in(series_ids),
            )
            .order_by(Desc(ProductRelease.datereleased))
        )

    def getFilesForReleases(self, releases):
        """See `IProductReleaseSet`."""
        releases = list(releases)
        if len(releases) == 0:
            return EmptyResultSet()
        SignatureAlias = ClassAlias(LibraryFileAlias)
        return DecoratedResultSet(
            IStore(ProductReleaseFile)
            .using(
                ProductReleaseFile,
                Join(
                    LibraryFileAlias,
                    ProductReleaseFile.libraryfile_id == LibraryFileAlias.id,
                ),
                LeftJoin(
                    LibraryFileContent,
                    LibraryFileAlias.content == LibraryFileContent.id,
                ),
                Join(
                    ProductRelease,
                    ProductReleaseFile.productrelease_id == ProductRelease.id,
                ),
                LeftJoin(
                    SignatureAlias,
                    ProductReleaseFile.signature_id == SignatureAlias.id,
                ),
            )
            .find(
                (
                    ProductReleaseFile,
                    LibraryFileAlias,
                    LibraryFileContent,
                    ProductRelease,
                    SignatureAlias,
                ),
                ProductReleaseFile.productrelease_id.is_in(
                    [release.id for release in releases]
                ),
            )
            .order_by(Desc(ProductReleaseFile.date_uploaded)),
            result_decorator=itemgetter(0),
        )


def productrelease_to_milestone(productrelease):
    """Adapt an `IProductRelease` to an `IMilestone`."""
    return productrelease.milestone
