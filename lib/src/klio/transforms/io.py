# Copyright 2020 Spotify AB

import json
import os

import apache_beam as beam

from apache_beam.io import avroio as beam_avroio
from apache_beam.io.gcp import bigquery as beam_bq
from apache_beam.io.gcp import bigquery_tools as beam_bq_tools

from klio_core.proto import klio_pb2


class BaseKlioIOException(Exception):
    """Base IO exception."""


class KlioMissingConfiguration(BaseKlioIOException):
    """Required configuration is missing."""


class _KlioReadWrapper(beam.io.Read):
    """Klio-ified `beam.io.Read` class.

    Handle the case if a user invokes an already-wrapped Klio IO
    transform (via `_KlioTransformMixin`) with `beam.io.Read`, as this
    is not possible to double-wrap.
    """

    def __init__(self, *args, **kwargs):
        if not len(args):
            # I would think we'd never get here, but this is just in case
            # something real funky happens, like this semi-private class
            # is not instantiated with an IO transform
            super(_KlioReadWrapper, self).__init__(*args, **kwargs)

        self.__wrapped_transform = args[0].__class__.__name__
        super(_KlioReadWrapper, self).__init__(*args, **kwargs)

    def is_bounded(self, *args, **kwargs):
        # beam.io.Read invokes beam.io.Read.source.is_bounded
        # (beam.io.Read.source == self/this instance) and throw an
        # AttributeError if is_bounded not defined. Here we define
        # the method but raise our own expection telling the user not to
        # invoke an alread-wrapped Klio IO Transform with `beam.io.Read()`
        raise TypeError(
            "Error reading from `{}`. If the transform was wrapped with "
            "`apache_beam.io.Read`, remove that wrapper and try again.".format(
                self.__wrapped_transform
            )
        )


class _KlioWrapIOMetaclass(type):
    def __call__(self, *args, **kwargs):
        # Some IO transforms require invocation to be wrapped with
        # `beam.io.Read()`, and others do not. This allows for the API
        # to be the same no matter what by wrapping `beam.io.Read` if
        # the transform requires it. If a user wraps a Klio IO transform
        # again (i.e. `beam.io.Read(_AlreadyWrappedKlioTransform))`), a
        # human-friendly `TypeError` will raise (see
        # `_KlioReadWrapper.is_bounded`)
        if self._REQUIRES_IO_READ_WRAP:
            return _KlioReadWrapper(
                super(_KlioWrapIOMetaclass, self).__call__(*args, **kwargs)
            )
        return super(_KlioWrapIOMetaclass, self).__call__(*args, **kwargs)


class _KlioTransformMixin(metaclass=_KlioWrapIOMetaclass):
    """Common properties for klio v2 IO transforms."""

    # whether or not the transform needs to be invoked by beam.io.Read()
    _REQUIRES_IO_READ_WRAP = False


class _KlioReadFromTextSource(beam.io.textio._TextSource):
    """Parses a text file as newline-delimited elements.
       Supports newline delimiters '\n' and '\r\n

    Returns:
        (str) KlioMessage serialized as a string
    """

    def read_records(self, file_name, range_tracker):
        records = super(_KlioReadFromTextSource, self).read_records(
            file_name, range_tracker
        )

        for record in records:
            record_as_bytes = record.encode("utf-8")
            message = klio_pb2.KlioMessage()
            message.data.v2.element = record_as_bytes
            yield message.SerializeToString()


class KlioReadFromText(beam.io.ReadFromText, _KlioTransformMixin):
    _source_class = _KlioReadFromTextSource


class _KlioBigQueryReader(beam_bq_tools.BigQueryReader):
    def __init__(self, *args, klio_message_columns=None, **kwargs):
        super(_KlioBigQueryReader, self).__init__(*args, **kwargs)
        self.__klio_message_columns = klio_message_columns

    def __generate_klio_message(self):
        message = klio_pb2.KlioMessage()
        message.version = klio_pb2.Version.V2

        # TODO: this is where we should add (relevant) KlioMessage.metadata;
        # (1) One thing to figure out is the klio_pb2.KlioJob definition,
        # particularly the JobInput definition, in light of KlioConfig v2.
        # Once that's figured out, we should at least populate the
        # job audit log.
        # (2) Another thing to figure out is force/ping. In streaming, messages
        # are individually marked as force or ping when needed. However,
        # users aren't able to tag individual messages generated from a row
        # of BQ data as force/ping, and it's probably very difficult for us
        # to provide a way to do that. So, should we allow users to at least
        # globally set force/ping on their event input config in klio-job.yaml?
        # Potentially.
        return message

    def __iter__(self):
        # NOTE: this assumes that the coder being used (default is
        # beam.io.gcp.bigquery_tools.RowAsDictJsonCoder, otherwise set in
        # klio-job.yaml) is JSON serializable (since the default is just
        # a plain dictionary). This assumption might break if someone
        # provides a different coder.
        # NOTE: We need to have the row elements be bytes, so if it is
        # a dictionary, we json.dumps into a str to convert to bytes,
        # but that may need to change if we want to support other coders
        for row in super(_KlioBigQueryReader, self).__iter__():
            message = self.__generate_klio_message()

            data = {}
            if self.__klio_message_columns:
                if len(self.__klio_message_columns) == 1:
                    data = row[self.__klio_message_columns[0]]

                else:
                    for key, value in row.items():
                        if key in self.__klio_message_columns:
                            data[key] = value
                    data = json.dumps(data)

            else:
                data = json.dumps(row)

            message.data.v2.element = bytes(data, "utf-8")
            yield message.SerializeToString()


class KlioReadFromBigQuery(beam_bq.BigQuerySource, _KlioTransformMixin):
    """Read from BigQuery with each row as a KlioMessage."""

    _REQUIRES_IO_READ_WRAP = True

    def __init__(self, *args, klio_message_columns=None, **kwargs):
        super(KlioReadFromBigQuery, self).__init__(*args, **kwargs)
        self.__klio_message_columns = klio_message_columns

    def reader(self, test_bigquery_client=None):
        return _KlioBigQueryReader(
            source=self,
            test_bigquery_client=test_bigquery_client,
            use_legacy_sql=self.use_legacy_sql,
            flatten_results=self.flatten_results,
            kms_key=self.kms_key,
            klio_message_columns=self.__klio_message_columns,
        )


class KlioWriteToBigQuery(beam.io.WriteToBigQuery, _KlioTransformMixin):
    """Writes to BigQuery table.

        Note: Not using BigQuerySink due to it only being available for
        batch. See https://beam.apache.org/releases/pydoc/2.22.0/
        apache_beam.io.gcp.bigquery.html?highlight=bigquerysink
        #apache_beam.io.gcp.bigquery.BigQuerySink
    """

    _REQUIRES_IO_READ_WRAP = False

    def __unwrap(self, encoded_element):
        message = klio_pb2.KlioMessage()
        message.ParseFromString(encoded_element)
        data = json.loads(message.data.v2.payload)

        return data

    def expand(self, pcoll):
        return super().expand(pcoll | beam.Map(self.__unwrap))


class _KlioTextSink(beam.io.textio._TextSink):
    """A :class:`~apache_beam.transforms.ptransform.PTransform`
       for writing to text files. Takes a PCollection of KlioMessages
       and writes the elements to a textfile
    """

    def write_record(self, file_handle, encoded_element):
        """Writes a single encoded record.
        Args:
            file_handle (str): a referential identifier that points to an
                audio file found in the configured output data location.
            encoded_element (KlioMessage): KlioMessage
        """
        message = klio_pb2.KlioMessage()
        message.ParseFromString(encoded_element)
        record = message.data.v2.element
        super(_KlioTextSink, self).write_encoded_record(file_handle, record)


class KlioWriteToText(beam.io.textio.WriteToText):
    def __init__(self, *args, **kwargs):
        self._sink = _KlioTextSink(*args, **kwargs)


# note: fast avro is default for py3 on beam
class _KlioFastAvroSource(beam_avroio._FastAvroSource):
    def read_records(self, file_name, range_tracker):
        records = super(_KlioFastAvroSource, self).read_records(
            file_name=file_name, range_tracker=range_tracker
        )
        for record in records:
            message = klio_pb2.KlioMessage()
            message.version = klio_pb2.Version.V2
            message.data.v2.element = bytes(json.dumps(record).encode("utf-8"))
            yield message.SerializeToString()


# define an I/O transform using the klio-specific avro source
# note: fast avro is default for py3 on beam
class KlioReadFromAvro(beam.io.ReadFromAvro):
    _REQUIRES_IO_READ_WRAP = True

    def __init__(
        self,
        file_pattern=None,
        location=None,
        show_unpublished=False,
        min_bundle_size=0,
        validate=True,
    ):
        file_pattern = self._get_file_pattern(
            file_pattern, location, show_unpublished
        )

        super(KlioReadFromAvro, self).__init__(
            file_pattern=file_pattern,
            min_bundle_size=min_bundle_size,
            validate=validate,
            use_fastavro=True,
        )

        self._source = _KlioFastAvroSource(
            file_pattern, min_bundle_size, validate=validate
        )

    def _get_file_pattern(self, file_pattern, location, show_unpublished):
        # TODO: this should be a validator in klio_core.config
        if not any([file_pattern, location]):
            raise KlioMissingConfiguration(
                "Must configure at least one of the following keys when "
                "reading from avro: `file_pattern`, `location`."
            )

        if all([file_pattern, location]):
            file_pattern = os.path.join(location, file_pattern)

        elif file_pattern is None:
            file_pattern = location

        return file_pattern