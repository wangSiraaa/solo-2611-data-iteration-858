"""Exception types shared by the semantics service and the delivery library."""

from __future__ import annotations

from typing import Iterable, Mapping, Any


class ValidationError(ValueError):
    """A request is malformed or violates a contract."""


class BatchRejected(Exception):
    """An observation admission batch was rejected atomically."""

    def __init__(self, reasons: Iterable[Mapping[str, Any]]):
        self.reasons = list(reasons)
        super().__init__("observation batch rejected: " + repr(self.reasons))


class ReferenceConflict(Exception):
    """A reference version already exists with different immutable content."""


class ReportConflict(Exception):
    """A report id already exists with different immutable content."""


class ReferenceNotFound(KeyError):
    pass


class ReportNotFound(KeyError):
    pass


class ProductConflict(Exception):
    """A product id already exists with different immutable content."""


class ProductNotFound(KeyError):
    pass


class DeliveryNotFound(KeyError):
    pass


class DeliveryIdConflict(Exception):
    """A successful delivery id is being reused with a different request."""


class DeliveryRejected(Exception):
    """No legal delivery exists for at least one requested record.

    A rejected task publishes no delivery rows.  ``attempt_id`` identifies the
    audit entry (if one was recorded), not a successful delivery identity.
    """

    def __init__(
        self,
        delivery_id: str,
        reasons: Iterable[Mapping[str, Any]],
        attempt_id: int | None = None,
    ):
        self.delivery_id = delivery_id
        self.reasons = list(reasons)
        self.attempt_id = attempt_id
        super().__init__(
            f"delivery {delivery_id!r} rejected: " + repr(self.reasons)
        )


class IntegrityError(Exception):
    """Stored immutable content failed independent read-back verification."""


# ---------------------------------------------------------------------------
# thermal cycle closure
# ---------------------------------------------------------------------------

class ThermalCatalogConflict(Exception):
    """A thermal catalog version already exists with different content."""


class ThermalCatalogNotFound(KeyError):
    pass


class ThermalProductConflict(Exception):
    """A thermal product id already exists with different immutable content."""


class ThermalProductNotFound(KeyError):
    pass


class ThermalSegmentRejected(Exception):
    """A thermal product build was rejected because a reading is non-finite.

    Nothing is published and any previously frozen product under the same id
    remains untouched.  ``attempt_id`` identifies the audit entry.
    """

    def __init__(
        self,
        product_id: str,
        reasons: Iterable[Mapping[str, Any]],
        attempt_id: int | None = None,
    ):
        self.product_id = product_id
        self.reasons = list(reasons)
        self.attempt_id = attempt_id
        super().__init__(
            f"thermal product {product_id!r} rejected: " + repr(self.reasons)
        )


# ---------------------------------------------------------------------------
# physical sample carousel / traceability
# ---------------------------------------------------------------------------

class StationLogConflict(Exception):
    """A station log version already exists with different content."""


class StationLogNotFound(KeyError):
    pass


class CarouselConfigConflict(Exception):
    """A carousel configuration id already exists with different content."""


class CarouselConfigNotFound(KeyError):
    pass


class ReconciliationConflict(Exception):
    """A reconciliation id exists for a different request."""


class ReconciliationNotFound(KeyError):
    pass


class PhysicalTraceConflict(Exception):
    """A recorded physical operation contradicts the carousel state.

    The first physical mismatch is reported and no reconciliation is published;
    previously published reconciliations remain readable.
    """

    def __init__(
        self,
        reconciliation_id: str,
        mismatch: Mapping[str, Any],
        attempt_id: int | None = None,
    ):
        self.reconciliation_id = reconciliation_id
        self.mismatch = dict(mismatch)
        self.attempt_id = attempt_id
        super().__init__(
            f"reconciliation {reconciliation_id!r} physical conflict: {self.mismatch!r}"
        )
