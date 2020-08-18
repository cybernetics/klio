# Copyright 2020 Spotify AB

import enum
import functools
import os

import apache_beam as beam

from apache_beam import pvalue
from apache_beam.io.gcp import gcsio

from klio.message_handler import v2 as v2_msg_handler
from klio.transforms import _utils
from klio.transforms import core


class DataExistState(enum.Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"

    # to human-friendly strings for easy logging
    @classmethod
    def to_str(cls, attr):
        if attr == cls.NOT_FOUND:
            return "not found"
        return "found"


# TODO mabe use klio core config's defs but make them strings
class KlioIODirection(enum.Enum):
    INPUT = "input"
    OUTPUT = "output"


# TODO: maybe use common.MessageState which then should be mapped to
# strings instead of ints (or have a method that does the conversion)
class TaggedStates(enum.Enum):
    PROCESS = "process"
    PASS_THRU = "pass_thru"
    DEFAULT = "tag_not_set"


# Only serializes to a KlioMessage; we deserialize within the process
# method itself since we also have to tag the output (too difficult to
# serialize output that's already tagged)
def _wrap_process(meth):
    @functools.wraps(meth)
    def wrapper(self, incoming_item, *args, **kwargs):
        try:
            kmsg = v2_msg_handler._to_klio_message(
                incoming_item, self._klio.config, self._klio.logger
            )
            yield from meth(self, kmsg, *args, **kwargs)

        except Exception as err:
            self._klio.logger.error(
                "Dropping KlioMessage - exception occurred when serializing "
                "'%s' to a KlioMessage.\nError: %s" % (incoming_item, err),
                exc_info=True,
            )
            return

    return wrapper


class _KlioBaseDoFnMetaclass(type):
    """Enforce behavior upon subclasses of `_KlioBaseDataExistenceCheck`."""

    def __init__(cls, name, bases, clsdict):
        if not getattr(cls, "_klio", None):
            setattr(cls, "_klio", core.KlioContext())

        if os.getenv("KLIO_TEST_MODE", "").lower() in ("true", "1"):
            return

        # TODO: fixme: not every child class will inherit from
        # _KlioBaseDataExistenceCheck
        if _utils.is_original_process_func(
            clsdict, bases, base_class="_KlioBaseDataExistenceCheck"
        ):
            # TODO: remove me when we no longer support config v1
            if cls._klio.config.version != 2:
                raise RuntimeError(
                    "Transform {} does not support 'version' declared in "
                    "'klio-job.yaml': {}".format(
                        name, cls._klio.config.version
                    )
                )

            setattr(cls, "process", _wrap_process(clsdict["process"]))

            cls._klio._transform_name = name

    def __call__(self, *args, **kwargs):
        # automatically wrap DoFn in a beam.ParDo (with or without
        # `with_outputs` for tagged outputs) so folks can just do
        # `pcoll | KlioInputDataExistenceCheck()` rather than
        # `pcoll | beam.ParDo(KlioInputDataExistenceCheck()).with_outputs()`
        if self.WITH_OUTPUTS is True:
            return beam.ParDo(
                super(_KlioBaseDoFnMetaclass, self).__call__(*args, **kwargs)
            ).with_outputs()

        return beam.ParDo(
            super(_KlioBaseDoFnMetaclass, self).__call__(*args, **kwargs)
        )


class _KlioBaseDataExistenceCheck(beam.DoFn, metaclass=_KlioBaseDoFnMetaclass):
    """Base class for data existence checking."""

    DIRECTION_PFX = None  # i.e. KlioIODirection.INPUT
    WITH_OUTPUTS = True

    @property
    def _location(self):
        return self._data_config.location

    @property
    def _suffix(self):
        return self._data_config.file_suffix

    @property
    def _data_config(self):
        pass

    def exists(self, *args, **kwargs):
        pass

    def _get_absolute_path(self, element):
        return os.path.join(
            self._location, element.decode("utf-8") + self._suffix
        )


class _KlioInputDataMixin(object):
    """Mixin to add input-specific logic for a data existence check.

    Must be used with _KlioGcsCheckExistsBase
    """

    DIRECTION_PFX = KlioIODirection.INPUT

    @property
    def _data_config(self):
        # TODO: figure out how to support multiple inputs
        return self._klio.config.job_config.data_inputs[0]


class _KlioOutputDataMixin(object):
    """Mixin to add output-specific logic for a data existence check.

    Must be used with _KlioGcsCheckExistsBase
    """

    DIRECTION_PFX = KlioIODirection.OUTPUT

    @property
    def _data_config(self):
        # TODO: figure out how to support multiple outputs
        return self._klio.config.job_config.data_outputs[0]


class _KlioGcsDataExistsMixin(object):
    """Mixin for GCS-specific data existence check logic.

    Must be used with _KlioBaseDataExistenceCheck and either
    _KlioInputDataMixin or _KlioOutputDataMixin
    """

    def setup(self):
        self.client = gcsio.GcsIO()

    def exists(self, path):
        return self.client.exists(path)


class _KlioGcsCheckExistsBase(
    _KlioGcsDataExistsMixin, _KlioBaseDataExistenceCheck
):
    """Must be used with either _KlioInputDataMixin or _KlioOutputDataMixin"""

    def process(self, kmsg):
        item = kmsg.data.v2.element
        item_path = self._get_absolute_path(item)
        item_exists = self.exists(item_path)

        state = DataExistState.FOUND
        if not item_exists:
            state = DataExistState.NOT_FOUND

        self._klio.logger.info(
            "%s %s at %s"
            % (
                self.DIRECTION_PFX.value.title(),
                DataExistState.to_str(state),
                item_path,
            )
        )

        # double tag for easier user interface, i.e. pcoll.found vs pcoll.true
        yield pvalue.TaggedOutput(state.value, kmsg.SerializeToString())