# -*- coding: utf-8 -*-

# Copyright 2014-2022 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

import sys
import json
import logging
from . import version, config, option, output, extractor, job, util, exception

__author__ = "Mike Fährmann"
__copyright__ = "Copyright 2014-2022 Mike Fährmann"
__license__ = "GPLv2"
__maintainer__ = "Mike Fährmann"
__email__ = "mike_faehrmann@web.de"
__version__ = version.__version__


def progress(urls, pformat):
    """Wrapper around urls to output a simple progress indicator"""
    if pformat is True:
        pformat = "[{current}/{total}] {url}\n"
    else:
        pformat += "\n"

    pinfo = {"total": len(urls)}
    for pinfo["current"], pinfo["url"] in enumerate(urls, 1):
        output.stderr_write(pformat.format_map(pinfo))
        yield pinfo["url"]


def parse_inputfile(file, log):
    """Filter and process strings from an input file.

    Lines starting with '#' and empty lines will be ignored.
    Lines starting with '-' will be interpreted as a key-value pair separated
      by an '='. where 'key' is a dot-separated option name and 'value' is a
      JSON-parsable value for it. These config options will be applied while
      processing the next URL.
    Lines starting with '-G' are the same as above, except these options will
      be valid for all following URLs, i.e. they are Global.
    Everything else will be used as potential URL.

    Example input file:

    # settings global options
    -G base-directory = "/tmp/"
    -G skip = false

    # setting local options for the next URL
    -filename="spaces_are_optional.jpg"
    -skip    = true

    https://example.org/

    # next URL uses default filename and 'skip' is false.
    https://example.com/index.htm
    """
    gconf = []
    lconf = []

    for line in file:
        line = line.strip()

        if not line or line[0] == "#":
            # empty line or comment
            continue

        elif line[0] == "-":
            # config spec
            if len(line) >= 2 and line[1] == "G":
                conf = gconf
                line = line[2:]
            else:
                conf = lconf
                line = line[1:]

            key, sep, value = line.partition("=")
            if not sep:
                log.warning("input file: invalid <key>=<value> pair: %s", line)
                continue

            try:
                value = json.loads(value.strip())
            except ValueError as exc:
                log.warning("input file: unable to parse '%s': %s", value, exc)
                continue

            section, sep, key = key.strip().rpartition(":")
            conf.append((section if sep else "__global__", key, value))

        else:
            # url
            if gconf or lconf:
                yield util.ExtendedUrl(line, gconf, lconf)
                gconf = []
                lconf = []
            else:
                yield line


def main():
    try:
        if sys.stdout and sys.stdout.encoding.lower() != "utf-8":
            output.replace_std_streams()

        parser = option.build_parser()
        args = parser.parse_args()
        log = output.initialize_logging(args.loglevel)

        if args.config:
            config._warn_legacy = False

        # configuration
        if args.load_config:
            config.load()
        if args.cfgfiles:
            config.load(args.cfgfiles, strict=True)
        if args.yamlfiles:
            config.load(args.yamlfiles, strict=True, fmt="yaml")
        if args.filename:
            filename = args.filename
            if filename == "/O":
                filename = "{filename}.{extension}"
            elif filename.startswith("\\f"):
                filename = "\f" + filename[2:]
            config.set("__global__", "filename", filename)
        if args.directory:
            config.set("__global__", "base-directory", args.directory)
            config.set("__global__", "directory", ())
        if args.postprocessors:
            config.set("__global__", "postprocessors", args.postprocessors)
        if args.abort:
            config.set("__global__", "skip", f"abort:{args.abort}")
        if args.terminate:
            config.set("__global__", "skip", f"terminate:{args.terminate}")
        if args.cookies_from_browser:
            browser, _, profile = args.cookies_from_browser.partition(":")
            browser, _, keyring = browser.partition("+")
            config.set("__global__", "cookies", (browser, profile, keyring))
        for opts in args.options:
            config.set(*opts)

        # signals
        signals = config.interpolate(("general",), "signals-ignore")
        if signals:
            import signal
            if isinstance(signals, str):
                signals = signals.split(",")
            for signal_name in signals:
                signal_num = getattr(signal, signal_name, None)
                if signal_num is None:
                    log.warning("signal '%s' is not defined", signal_name)
                else:
                    signal.signal(signal_num, signal.SIG_IGN)

        # extractor modules
        modules = config.interpolate(("general", "extractor"), "modules")
        if modules is not None:
            if isinstance(modules, str):
                modules = modules.split(",")
            extractor.modules = modules
            extractor._module_iter = iter(modules)

        # loglevels
        output.configure_logging(args.loglevel)
        if args.loglevel >= logging.ERROR:
            config.set(("output",), "mode", "null")
        elif args.loglevel <= logging.DEBUG:
            import platform
            import subprocess
            import os.path
            import requests

            extra = ""
            if getattr(sys, "frozen", False):
                extra = " - Executable"
            else:
                try:
                    out, err = subprocess.Popen(
                        ("git", "rev-parse", "--short", "HEAD"),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=os.path.dirname(os.path.abspath(__file__)),
                    ).communicate()
                    if out and not err:
                        extra = " - Git HEAD: " + out.decode().rstrip()
                except (OSError, subprocess.SubprocessError):
                    pass

            log.debug("Version %s%s", __version__, extra)
            log.debug("Python %s - %s",
                      platform.python_version(), platform.platform())
            try:
                log.debug("requests %s - urllib3 %s",
                          requests.__version__,
                          requests.packages.urllib3.__version__)
            except AttributeError:
                pass

        if args.list_modules:
            extractor.modules.append("")
            sys.stdout.write("\n".join(extractor.modules))

        elif args.list_extractors:
            write = sys.stdout.write
            fmt = "{}\n{}\nCategory: {} - Subcategory: {}{}\n\n".format

            for extr in extractor.extractors():
                if not extr.__doc__:
                    continue
                test = next(extr._get_tests(), None)
                write(fmt(
                    extr.__name__, extr.__doc__,
                    extr.category, extr.subcategory,
                    "\nExample : " + test[0] if test else "",
                ))

        elif args.clear_cache:
            from . import cache
            log = logging.getLogger("cache")
            cnt = cache.clear(args.clear_cache)

            if cnt is None:
                log.error("Database file not available")
            else:
                log.info(
                    "Deleted %d %s from '%s'",
                    cnt, "entry" if cnt == 1 else "entries", cache._path(),
                )
        elif args.config:
            if not args.load_config:
                del config._default_configs[:]
            if args.cfgfiles:
                config._default_configs.extend(args.cfgfiles)
            if args.config == "init":
                return config.config_init()
            if args.config == "open":
                return config.config_open()
            if args.config == "status":
                return config.config_status()
            if args.config == "update":
                return config.config_update()
        else:
            if not args.urls and not args.inputfiles:
                parser.error(
                    "The following arguments are required: URL\n"
                    "Use 'gallery-dl --help' to get a list of all options.")

            if args.list_urls:
                jobtype = job.UrlJob
                jobtype.maxdepth = args.list_urls
                if config.get("output", "fallback", True):
                    jobtype.handle_url = \
                        staticmethod(jobtype.handle_url_fallback)
            else:
                jobtype = args.jobtype or job.DownloadJob

            urls = args.urls
            if args.inputfiles:
                for inputfile in args.inputfiles:
                    try:
                        if inputfile == "-":
                            if sys.stdin:
                                urls += parse_inputfile(sys.stdin, log)
                            else:
                                log.warning(
                                    "input file: stdin is not readable")
                        else:
                            with open(inputfile, encoding="utf-8") as file:
                                urls += parse_inputfile(file, log)
                    except OSError as exc:
                        log.warning("input file: %s", exc)

            # unsupported file logging handler
            handler = output.setup_logging_handler(
                "unsupportedfile", fmt="{message}")
            if handler:
                ulog = logging.getLogger("unsupported")
                ulog.addHandler(handler)
                ulog.propagate = False
                job.Job.ulog = ulog

            pformat = config.get("output", "progress", True)
            if pformat and len(urls) > 1 and args.loglevel < logging.ERROR:
                urls = progress(urls, pformat)

            retval = 0
            for url in urls:
                try:
                    log.debug("Starting %s for '%s'", jobtype.__name__, url)
                    if isinstance(url, util.ExtendedUrl):
                        for opts in url.gconfig:
                            config.set(*opts)
                        with config.apply(url.lconfig):
                            retval |= jobtype(url.value).run()
                    else:
                        retval |= jobtype(url).run()
                except exception.TerminateExtraction:
                    pass
                except exception.NoExtractorError:
                    log.error("No suitable extractor found for '%s'", url)
                    retval |= 64
            return retval

    except KeyboardInterrupt:
        sys.exit("\nKeyboardInterrupt")
    except BrokenPipeError:
        pass
    except OSError as exc:
        import errno
        if exc.errno != errno.EPIPE:
            raise
    return 1
