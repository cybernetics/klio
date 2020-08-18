# -*- coding: utf-8 -*-
# Copyright 2019 Spotify AB

from __future__ import absolute_import

import json
import logging
import os

import docker
import emoji
import requests

from klio_cli.utils import multi_line_terminal_writer


def check_docker_connection(docker_client):
    try:
        docker_client.ping()
    except (docker.errors.APIError, requests.exceptions.ConnectionError):
        logging.error(emoji.emojize("Could not reach Docker! :whale:"))
        logging.error("Is it installed and running?")
        raise SystemExit(1)


def check_dockerfile_present(job_dir):
    dockerfile_path = job_dir + "/Dockerfile"
    if not os.path.exists(dockerfile_path):
        logging.error("Klio can't run job without a Dockerfile.")
        logging.error("Please supply \033[4m{}\033[4m".format(dockerfile_path))
        raise SystemExit(1)


def docker_image_exists(name, client):
    try:
        client.images.get(name)
        exists = True
    except docker.errors.ImageNotFound:
        exists = False
    except docker.errors.APIError as e:
        msg = (
            "Docker ran into the error checking if image {}"
            "has already been built:\n{}".format(name, e)
        )
        logging.error(msg)
        raise SystemExit(1)
    return exists


def build_docker_image(job_dir, image_name, image_tag, config_file=None):
    """Build given Docker image.

    Note: This uses the python Docker SDK's low-level API in order to capture
    and emit build logs as they are generated by Docker. Using the
    high-level API, you only get access to logs at the end of the build,
    which creates a bad user experience.

    Args:
        job_dir (str): Relative path to directory containing Dockerfile.
        image_name (str): Name to build the image with (forms a ‘name:tag’ pair)
        image_tag (str): Tag to build the image with (forms a ‘name:tag’ pair)
    Raises:
        SystemExit(1) If Docker build errors out, process terminates.
    """

    def clean_logs(log_generator):
        # Loop through lines containing log JSON objects.
        # Example line: {"stream":"Starting build..."}\r\n{"stream":"\\n"}\n
        for line in log_generator:
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            # Some lines contain multiple whitespace-separated objects.
            # Split them so json.loads doesn't choke.
            for log_obj in line.split("\r\n"):
                # Some log objects only wrap newlines.
                # Split sometimes produces '' char.
                # Remove these artifacts.
                if log_obj != '{"stream":"\\n"}' and log_obj != "":
                    yield log_obj

    def print_log(log):
        if "stream" in log:
            logging.info(log["stream"].strip("\n"))
        if "error" in log:
            fail_color = "\033[91m"
            end_color = "\033[0m"
            logging.info(
                "{}{}{}".format(
                    fail_color, log["errorDetail"]["message"], end_color
                )
            )
            logging.error("\nDocker hit an error while building job image.")
            logging.error(
                "Please fix your Dockerfile: {}/Dockerfile".format(job_dir)
            )
            raise SystemExit(1)

    build_flag = {
        "path": job_dir,
        "tag": "{}:{}".format(image_name, image_tag),
        "rm": True,
        "buildargs": {
            "tag": image_tag,
            "KLIO_CONFIG": config_file or "klio-job.yaml",
        },
    }  # Remove intermediate build containers.
    logs = docker.APIClient(base_url="unix://var/run/docker.sock").build(
        **build_flag
    )

    for log_obj in clean_logs(logs):
        log = json.loads(log_obj)
        print_log(log)


def _get_layer_id_and_message(clean_line):
    line_json = json.loads(clean_line)
    layer_id = line_json.get("id")
    # very first log message doesn't have an id
    msg_pfx = ""
    if layer_id:
        msg_pfx = "{}: ".format(layer_id)
    msg = "{prefix}{status}{progress}".format(
        prefix=msg_pfx,
        status=line_json.get("status", ""),
        progress=line_json.get("progress", ""),
    )
    return layer_id, msg


def push_image_to_gcr(image, tag, client):
    kwargs = {"repository": image, "tag": tag, "stream": True}
    writer = multi_line_terminal_writer.MultiLineTerminalWriter()
    for raw_line in client.images.push(**kwargs):
        clean_line = raw_line.decode("utf-8").strip("\r\n")
        clean_lines = clean_line.split("\r\n")

        for line in clean_lines:
            layer_id, msg = _get_layer_id_and_message(line)
            writer.emit_line(layer_id, msg.strip())


def get_docker_image_client(job_dir, image_tag, image_name, force_build):
    """Returns the docker image and client for running klio commands.
    Args:
        job_dir (str): Relative path to directory containing Dockerfile.
        image_tag (str): Tag to build the image with (forms a ‘name:tag’ pair)
        image_name (str): Name to build the image with (forms a ‘name:tag’ pair)
        force_build(bool): Flag to force a new docker image build.
    Raises:
        Valid docker image and client.
    """
    image = "{}:{}".format(image_name, image_tag)
    client = docker.from_env()
    check_docker_connection(client)
    check_dockerfile_present(job_dir)
    if not docker_image_exists(image, client) or force_build:
        logging.info("Building worker image: {}".format(image))
        build_docker_image(job_dir, image_name, image_tag)
    else:
        logging.info("Found worker image: {}".format(image))
    return image, client