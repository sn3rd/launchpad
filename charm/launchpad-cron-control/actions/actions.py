#! /usr/bin/python3
# Copyright 2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os.path
import subprocess
import sys
import traceback
from configparser import ConfigParser, NoSectionError
from pathlib import Path
from typing import Sequence

sys.path.append("lib")

from charms.layer import basic  # noqa: E402

basic.bootstrap_charm_deps()
basic.init_config_states()

from charmhelpers.core import hookenv  # noqa: E402

config_path = "/srv/launchpad/www/cron.ini"


def read_config() -> ConfigParser:
    config = ConfigParser({"enabled": "True"})
    config.read(config_path)
    return config


def write_config(config: ConfigParser) -> None:
    with open(f"{config_path}.new", "w") as f:
        config.write(f)
    os.replace(f"{config_path}.new", config_path)


def set_config_option(
    config: ConfigParser, section: str, option: str, value: str
) -> None:
    """Set a config option, ensuring that its section exists."""
    if section != "DEFAULT" and not config.has_section(section):
        config.add_section(section)
    config.set(section, option, value)


def enable_cron() -> None:
    params = hookenv.action_get()
    config = read_config()
    if config.getboolean("DEFAULT", "enabled", fallback=False):
        # The default is already enabled.  Just make sure that we aren't
        # overriding the default.
        try:
            config.remove_option(params["job"], "enabled")
        except NoSectionError:
            pass
    else:
        set_config_option(config, params["job"], "enabled", "True")
    write_config(config)


def enable_cron_all() -> None:
    config = ConfigParser({"enabled": "True"})
    write_config(config)


def disable_cron() -> None:
    params = hookenv.action_get()
    config = read_config()
    set_config_option(config, params["job"], "enabled", "False")
    write_config(config)


def disable_cron_all() -> None:
    config = ConfigParser({"enabled": "False"})
    write_config(config)


def get_current_config() -> None:
    hookenv.log("Getting current cron-control configuration.")
    subprocess.run(
        ["/usr/bin/curl", "http://localhost/cron.ini"],
        check=True,
    )
    hookenv.action_set({"result": "Current configuration displayed."})


def main(argv: Sequence[str]) -> None:
    action = Path(argv[0]).name
    try:
        if action == "disable-cron":
            disable_cron()
        elif action == "disable-cron-all":
            disable_cron_all()
        elif action == "enable-cron":
            enable_cron()
        elif action == "enable-cron-all":
            enable_cron_all()
        elif action == "get-current-config":
            get_current_config()
        else:
            hookenv.action_fail(f"Action {action} not implemented.")
    except Exception:
        hookenv.action_fail("Unhandled exception")
        tb = traceback.format_exc()
        hookenv.action_set(dict(traceback=tb))
        hookenv.log(f"Unhandled exception in action {action}:")
        for line in tb.splitlines():
            hookenv.log(line)


if __name__ == "__main__":
    main(sys.argv)
