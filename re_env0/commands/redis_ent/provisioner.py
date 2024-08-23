import copy
import secrets
import enum

import typer
from rich import print
import requests

from ...console import console
from .client import RedisEnterpriseClient


class EndpointFormat(str, enum.Enum):
    redis_uri = "redis-uri"
    host_and_port = "host:port"


class REProvisioner(object):

    def __init__(self, api: RedisEnterpriseClient):
        self.api = api
        self.created_endpoints = {}
        self._roles_to_users = {}

    def provision(
        self,
        bdb_configs: dict,
        endpoint_format: EndpointFormat,
        clusters_config: dict = None,
    ):
        if isinstance(bdb_configs, dict):
            self._create_roles(bdb_configs.pop("roles", []))
            self._create_acls(bdb_configs.pop("acls", []))
            self._roles_to_users = self._create_users(bdb_configs.pop("users", []))

            crdb_configs = bdb_configs.pop("crdbs", [])

            if crdb_configs and clusters_config:
                self._create_crdbs(crdb_configs, clusters_config)

            bdb_configs = bdb_configs.pop("databases")

        self._create_bdbs(bdb_configs)

        return self.created_endpoints

    def _create_roles(self, roles):
        for role in roles:
            try:
                self.api.create_role(role)
            except requests.exceptions.RequestException as e:
                print(f"Error creating role {role['name']}: {e}")
                raise typer.Exit(code=1)

        print("Available roles: ", self.api.get_roles())

    def _create_acls(self, acls):
        for acl in acls:
            try:
                self.api.create_acl(acl)
            except requests.exceptions.RequestException as e:
                print(f"Error creating ACL {acl['name']}: {e}")
                raise typer.Exit(code=1)

    def _create_users(self, users):
        roles_to_users = {}
        for user in users:
            try:
                user["password"] = secrets.token_urlsafe(16)
                self.api.create_user(user)

                for role_id in user["role_uids"]:
                    roles_to_users[role_id] = {
                        "username": user["name"],
                        "password": user["password"],
                    }

            except requests.exceptions.RequestException as e:
                print(f"Error creating user {user['name']}: {e}")
                raise typer.Exit(code=1)

        return roles_to_users

    def _create_bdbs(self, bdb_configs):
        for bdb_config in bdb_configs:
            console.log(f"Creating BDB: {bdb_config['name']}")

            bdb_config, user_name, password = self._get_bdb_config_with_auth(bdb_config)

            try:
                bdb_object = self.api.create_bdb(bdb_config)

                self.created_endpoints[bdb_config["name"]] = {
                    "bdb_id": bdb_object["uid"],
                    "username": user_name,
                    "password": password,
                    "tls": bdb_object["ssl"],
                }

                console.log(
                    f"Created BDB: {bdb_config['name']} with ID: {bdb_object['uid']}"
                )
            except requests.exceptions.RequestException as e:
                print(f"Error creating BDB {bdb_config['name']}: {e}")
                print(f"Failed BDB config: {bdb_config}")
                print(f"Response: {e.response.text}")
                raise typer.Exit(code=1)

    def _create_crdbs(self, crdb_configs, clusters_config):
        clusters = []
        for c in clusters_config:
            clusters.append(
                {
                    "url": f"https://{c['cluster_name']}:9443",
                    "credentials": {
                        "username": c["username"],
                        "password": c["password"],
                    },
                    "name": c["cluster_name"],
                }
            )

        for crdb in crdb_configs:
            console.log(f"Creating CRDB: {crdb['name']}")

            crdb_config, user_name, password = self._get_bdb_config_with_auth(crdb)

            try:
                crdb_task = self.api.create_crdb(crdb_config, clusters)

                crdb_task = self.api.wait_for_crdb_task(crdb_task["id"])

                crdb_object = self.api.get_crdb(crdb_task["crdb_guid"])

                self.created_endpoints[crdb_config["name"]] = {
                    "bdb_id": crdb_object["local_databases"][0]["bdb_uid"],
                    "username": user_name,
                    "password": password,
                    "tls": crdb_config.get("ssl", False),
                }

                console.log(
                    f"Created CRDB: {crdb['name']} with ID: {crdb_object['guid']}"
                )
            except (requests.exceptions.RequestException, IndexError, KeyError) as e:
                print(f"Error creating CRDB {crdb['name']}: {e}")
                raise typer.Exit(code=1)

    def _get_bdb_config_with_auth(self, bdb_config):
        bdb_config = copy.deepcopy(bdb_config)

        if (
            "roles_permissions" in bdb_config
            and bdb_config.get("default_user", True) is False
        ):
            try:
                role_id = bdb_config["roles_permissions"][0]["role_uid"]
            except KeyError:
                print(f"Use 'role_permissions' to specify the role for the user")
                raise typer.Exit(code=1)
            try:
                user_name = self._roles_to_users[role_id]["username"]
                password = self._roles_to_users[role_id]["password"]
            except KeyError:
                print(
                    f"Role {role_id} is not defined in the roles section of the config file"
                )
                raise typer.Exit(code=1)
        else:
            user_name = "default"
            password = secrets.token_urlsafe(16)
            bdb_config["authentication_redis_pass"] = password

        return bdb_config, user_name, password
