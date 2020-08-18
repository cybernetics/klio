#!/usr/bin/env python
# Copyright 2019 Spotify AB
from __future__ import absolute_import

import collections
import imp
import logging
import os
import re

import apache_beam as beam
from apache_beam.options import pipeline_options

from klio import __version__ as klio_lib_version
from klio import transforms
from klio.transforms import helpers
from klio_core import __version__ as klio_core_version

from klio_exec import __version__ as klio_exec_version


DATAFLOW_LABEL_KEY_TO_OS_ENVIRON = {
    "build_id": "BUILD_ID",
    "organization": "ORGANIZATION",
    "repo": "REPOSITORY",
    "branch": "BRANCH_NAME",
    "commit_sha": "COMMIT_SHA",
    "klio-cli": "KLIO_CLI_VERSION",
}

# Regex according to (https://cloud.google.com/resource-manager/
# docs/creating-managing-labels#requirements)
# otherwise, deployments will fail
DATAFLOW_LABEL_REGEX = re.compile(r"([a-zA-Z0-9_\-]+)")
HERE = os.path.abspath(".")


# NOTE: hopefully we don't get an dict lookup errors since KlioConfig
# should raise if given an unsupported event IO transform
class StreamingEventMapper(object):
    input = {"pubsub": beam.io.ReadFromPubSub}
    output = {"pubsub": beam.io.WriteToPubSub}


class BatchEventMapper(object):
    input = {
        "file": transforms.KlioReadFromText,
        "bq": transforms.KlioReadFromBigQuery,
        "avro": transforms.KlioReadFromAvro,
    }
    output = {
        "file": transforms.KlioWriteToText,
        "bq": transforms.KlioWriteToBigQuery,
    }


class EventIOMapper(object):
    streaming = StreamingEventMapper()
    batch = BatchEventMapper()


class KlioPipeline(object):
    def __init__(self, job_name, config, runtime_conf):
        self.job_name = job_name
        self.config = config
        self.runtime_conf = runtime_conf

    @property
    def _has_event_inputs(self):
        return len(self.config.job_config.event_inputs) > 0

    @property
    def _has_multi_event_inputs(self):
        return len(self.config.job_config.event_inputs) > 1

    @property
    def _has_event_outputs(self):
        return len(self.config.job_config.event_outputs) > 0

    @property
    def _io_mapper(self):
        if not self.config.pipeline_options.streaming:
            return EventIOMapper.batch
        return EventIOMapper.streaming

    def _set_setup_options(self, options):
        setup_options = options.view_as(pipeline_options.SetupOptions)

        if setup_options.setup_file:
            setup_file_path = os.path.join(HERE, setup_options.setup_file)
            setup_options.setup_file = setup_file_path

        if setup_options.requirements_file:
            reqs_file_path = os.path.join(
                HERE, setup_options.requirements_file
            )
            setup_options.requirements_file = reqs_file_path

    def _set_debug_options(self, options):
        pass

    def _set_standard_options(self, options):
        standard_opts = options.view_as(pipeline_options.StandardOptions)

        if self.runtime_conf.direct_runner:
            standard_opts.runner = "direct"

    @staticmethod
    def _get_image_tag(image, tag):
        if not tag:
            return image
        image_name = image.split(":")[0]
        return "{}:{}".format(image_name, tag)

    def _set_worker_options(self, options):

        worker_opts = options.view_as(pipeline_options.WorkerOptions)

        fnapi_enabled = (
            "beam_fn_api" in self.config.pipeline_options.experiments
        )
        if worker_opts.worker_harness_container_image and fnapi_enabled:
            image = KlioPipeline._get_image_tag(
                worker_opts.worker_harness_container_image,
                self.runtime_conf.image_tag,
            )
            worker_opts.worker_harness_container_image = image

    @staticmethod
    def _get_clean_label_value(label_value):
        # Get all regex matches of the label value, replace any unsuppoted
        # character with "-", and limit to 63 lowercase characters
        matches = re.findall(DATAFLOW_LABEL_REGEX, label_value)
        if not matches:
            return
        ret_label = "-".join(matches)
        ret_label = ret_label[:63]  # max 63 chars
        return ret_label.lower()

    def _set_google_cloud_options(self, options):
        gcp_opts = options.view_as(pipeline_options.GoogleCloudOptions)

        gcp_opts.job_name = self.job_name

        if self.runtime_conf.update is not None:
            gcp_opts.update = self.runtime_conf.update

        labels = gcp_opts.labels or []

        klio_exec_value = KlioPipeline._get_clean_label_value(
            klio_exec_version
        )
        klio_core_value = KlioPipeline._get_clean_label_value(
            klio_core_version
        )
        klio_value = KlioPipeline._get_clean_label_value(klio_lib_version)
        klio_labels = [
            "klio-exec={}".format(klio_exec_value),
            "klio-core={}".format(klio_core_value),
            "klio={}".format(klio_value),
        ]

        labels.extend(klio_labels)

        # Dataflow may not be able to handle duplicate keys; we should probably
        # do that here in some fashion (@lynn)
        for label, os_key in DATAFLOW_LABEL_KEY_TO_OS_ENVIRON.items():
            os_value = os.environ.get(os_key, "")
            os_value = KlioPipeline._get_clean_label_value(os_value)
            if os_value:
                labels.append("{}={}".format(label, os_value))

        if os.environ.get("CI", "").lower() == "true":
            # TODO: maybe provide way to allow something besides just "CI"
            labels.append("deployed_by=CI")
        elif os.environ.get("USER"):
            labels.append("deployed_by={}".format(os.environ["USER"].lower()))

        gcp_opts.labels = labels

    def _parse_config_pipeline_options(self):
        pipe_opts_dict = self.config.pipeline_options.as_dict()
        has_setup_file = pipe_opts_dict.get("setup_file") is not None
        has_reqs_file = pipe_opts_dict.get("requirements_file") is not None
        if any([has_setup_file, has_reqs_file]):
            # Dataflow will complain of not supporting custom images if
            # setup_file/reqs_file are used (w/o the beam_fn_api experiment)
            pipe_opts_dict.pop("worker_harness_container_image", None)
        return dict((k, v) for k, v in pipe_opts_dict.items() if v is not None)

    def _get_pipeline_options(self):
        # Remove None values since the from_dictionary sets these as
        # a string 'None' for the PipelineOptions flags.
        config_pipeline_options = self._parse_config_pipeline_options()

        options = pipeline_options.PipelineOptions().from_dictionary(
            config_pipeline_options
        )

        self._set_google_cloud_options(options)
        self._set_worker_options(options)
        self._set_standard_options(options)
        self._set_debug_options(options)
        self._set_setup_options(options)

        return options

    def _get_run_callable(self):
        run_path = "./run.py"
        try:
            run_module = imp.load_source("run", run_path)
            run_basic_callable = getattr(run_module, "run_basic", None)
            run_callable = getattr(run_module, "run", None)

            if not any([run_callable, run_basic_callable]):
                msg = "No 'run' function in run.py of job {}".format(
                    self.job_name
                )
                logging.error(msg)
                raise SystemExit(1)
            return run_basic_callable if run_basic_callable else run_callable
        except (ImportError, IOError):
            logging.error(
                "Could not import run.py in job {}".format(self.job_name),
                exc_info=True,
            )
            raise SystemExit(1)

    def _verify_packaging(self):
        pipeline_opts = self.config.pipeline_options
        experiments = pipeline_opts.experiments
        fnapi_enabled = "beam_fn_api" in experiments
        has_setup_file = pipeline_opts.setup_file is not None
        has_reqs_file = pipeline_opts.requirements_file is not None
        if fnapi_enabled and any([has_setup_file, has_reqs_file]):
            logging.error(
                "The 'beam_fn_api' experiment may not be enabled while "
                "providing a setup.py file and/or a requirements.txt file."
            )
            raise SystemExit(1)

    def _setup_data_io_filters(self, in_pcol, label_prefix=None):
        # label prefixes are required for multiple inputs (to avoid label
        # name collisions in Beam)
        if (
            len(self.config.job_config.data_inputs) > 1
            or len(self.config.job_config.data_outputs) > 1
        ):
            logging.error(
                "Klio does not (yet) support multiple data inputs and outputs."
            )
            raise SystemExit(1)

        data_input_config = self.config.job_config.data_inputs[0]
        data_output_config = self.config.job_config.data_outputs[0]

        pfx = ""
        if label_prefix is not None:
            pfx = "[{}] ".format(label_prefix)

        def lbl(label):
            return "{}{}".format(pfx, label)

        pings = in_pcol | lbl("Ping Filter") >> helpers.KlioFilterPing()

        if not data_output_config.skip_klio_existence_check:
            output_exists = (
                pings.process
                | lbl("Output Exists Filter")
                >> helpers.KlioGcsCheckOutputExists()
            )
            output_force = (
                output_exists.found
                | lbl("Output Force Filter") >> helpers.KlioFilterForce()
            )
            to_pass_thru_tuple = (pings.pass_thru, output_force.pass_thru)
            to_pass_thru = (
                to_pass_thru_tuple
                | lbl("Flatten to Pass Thru") >> beam.Flatten()
            )

            to_filter_input_tuple = (
                output_exists.not_found,
                output_force.process,
            )
            to_filter_input = (
                to_filter_input_tuple
                | lbl("Flatten to Process") >> beam.Flatten()
            )
        else:
            to_pass_thru = pings.pass_thru
            to_filter_input = pings.process

        if not data_input_config.skip_klio_existence_check:
            input_exists = (
                to_filter_input
                | lbl("Input Exists Filter")
                >> helpers.KlioGcsCheckInputExists()
            )
            _ = (
                input_exists.not_found
                | lbl("Drop Not Found Data") >> helpers.KlioDrop()
            )
            to_process = input_exists.found
        else:
            to_process = to_filter_input

        return to_process, to_pass_thru

    # TODO this can prob go away if/when we make event_inputs a
    # dictionary rather than a list of dicts (@lynn)
    def _generate_input_conf_names(self):
        ev_inputs = self.config.job_config.event_inputs
        input_dict = {}
        for index, ev in enumerate(ev_inputs):
            name = "{}{}".format(ev.name, index)
            input_dict[name] = ev
        return input_dict

    def _generate_pcoll_per_input(self, pipeline):
        inputs = self._generate_input_conf_names()
        MultiInputPCollTuple = collections.namedtuple(
            "MultiInputPCollTuple", list(inputs.keys())
        )
        input_name_to_input_pcolls = {}
        multi_to_pass_thru = []
        for input_name, input_conf in inputs.items():
            input_to_process, input_to_pass_thru = self._generate_pcoll(
                pipeline, input_conf, label_prefix=input_name
            )
            if input_to_pass_thru:
                multi_to_pass_thru.append(input_to_pass_thru)
            input_name_to_input_pcolls[input_name] = input_to_process

        to_process = MultiInputPCollTuple(**input_name_to_input_pcolls)
        to_pass_thru = (
            multi_to_pass_thru
            | "Merge multi-input pass-thrus" >> beam.Flatten()
        )
        return to_process, to_pass_thru

    def _generate_pcoll(self, pipeline, input_config, label_prefix=None):
        to_pass_thru = None
        to_process = pipeline

        if input_config.skip_klio_read:
            return to_process, to_pass_thru

        label = "Read Event Input"
        if label_prefix:
            label = "[{}] {}".format(label_prefix, label)

        transform_cls_in = self._io_mapper.input[input_config.name]
        in_pcol = pipeline | label >> transform_cls_in(
            **input_config.to_io_kwargs()
        )
        to_process, to_pass_thru = self._setup_data_io_filters(
            in_pcol, label_prefix
        )
        return to_process, to_pass_thru

    # mutates the pipeline object, no need to return it
    def _setup_pipeline(self, pipeline):
        run_callable = self._get_run_callable()

        to_pass_thru = None
        to_process = pipeline
        # sanity check - I think klio config forces event input
        if self._has_event_inputs:
            if not self._has_multi_event_inputs:
                input_config = self.config.job_config.event_inputs[0]
                to_process, to_pass_thru = self._generate_pcoll(
                    pipeline, input_config
                )
            else:
                to_process, to_pass_thru = self._generate_pcoll_per_input(
                    pipeline
                )

        out_pcol = run_callable(to_process, self.config)

        if self._has_event_outputs:
            output_config = self.config.job_config.event_outputs[0]
            if not output_config.skip_klio_write:
                transform_cls_out = self._io_mapper.output[output_config.name]
                to_output = out_pcol
                if to_pass_thru:
                    to_output_tuple = (out_pcol, to_pass_thru)
                    to_output = (
                        to_output_tuple | "Flatten to Output" >> beam.Flatten()
                    )

                _ = to_output | transform_cls_out(
                    **output_config.to_io_kwargs()
                )

    def run(self):
        self._verify_packaging()
        options = self._get_pipeline_options()
        options.view_as(pipeline_options.SetupOptions).save_main_session = True

        pipeline = beam.Pipeline(options=options)

        self._setup_pipeline(pipeline)

        try:
            # NOTE: When running with Dataflow, this `result` object has a lot
            #       of information about the job (id, name, project, status,
            #       etc). Could be useful if wanting to report back the status,
            #       URL of the dataflow job, etc.  @lynn
            result = pipeline.run()
        except ValueError as e:
            if (
                self.runtime_conf.update
                and "No running job found with name" in str(e)
            ):
                # job is currently not running - should simply deploy without
                # updating set
                # TODO: is this possible?
                self.runtime_conf = self.runtime_conf._replace(update=None)
                return self.run()

            logging.error("Error running pipeline: %s" % e)
            raise SystemExit(1)

        if self.runtime_conf.direct_runner or self.runtime_conf.blocking:
            # the pipeline on direct runner will otherwise get garbage collected
            result.wait_until_finish()