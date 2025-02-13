import json
import logging
import threading
import time
from typing import Any, Dict, List

import requests
from postgrest_py.request_builder import QueryRequestBuilder
from supabase_py import Client
from supabase_py.lib.auth_client import SupabaseAuthClient

from robusta.core.model.cluster_status import ClusterStatus
from robusta.core.model.env_vars import SUPABASE_LOGIN_RATE_LIMIT_SEC
from robusta.core.model.helm_release import HelmRelease
from robusta.core.model.jobs import JobInfo
from robusta.core.model.namespaces import NamespaceInfo
from robusta.core.model.nodes import NodeInfo
from robusta.core.model.services import ServiceInfo
from robusta.core.reporting import Enrichment
from robusta.core.reporting.base import Finding
from robusta.core.reporting.blocks import ScanReportBlock, ScanReportRow
from robusta.core.reporting.consts import EnrichmentAnnotation
from robusta.core.sinks.robusta.dal.model_conversion import ModelConversion

SERVICES_TABLE = "Services"
NODES_TABLE = "Nodes"
EVIDENCE_TABLE = "Evidence"
ISSUES_TABLE = "Issues"
CLUSTERS_STATUS_TABLE = "ClustersStatus"
JOBS_TABLE = "Jobs"
HELM_RELEASES_TABLE = "HelmReleases"
NAMESPACES_TABLE = "Namespaces"
UPDATE_CLUSTER_NODE_COUNT = "update_cluster_node_count"
SCANS_RESULT_TABLE = "ScansResults"


class RobustaAuthClient(SupabaseAuthClient):
    def _set_timeout(*args, **kwargs):
        """Set timer task"""
        # _set_timeout isn't implemented in gotrue client. it's required for the jwt refresh token timer task
        # https://github.com/supabase/gotrue-py/blob/49c092e3a4a6d7bb5e1c08067a4c42cc2f74b5cc/gotrue/client.py#L242
        # callback, timeout_ms
        threading.Timer(args[2] / 1000, args[1]).start()


class RobustaClient(Client):
    def _get_auth_headers(self) -> Dict[str, str]:
        auth = getattr(self, "auth", None)
        session = auth.current_session if auth else None
        if session and session["access_token"]:
            access_token = auth.session()["access_token"]
        else:
            access_token = self.supabase_key

        headers: Dict[str, str] = {
            "apiKey": self.supabase_key,
            "Authorization": f"Bearer {access_token}",
        }
        return headers

    @staticmethod
    def _init_supabase_auth_client(
            auth_url: str,
            supabase_key: str,
            detect_session_in_url: bool,
            auto_refresh_token: bool,
            persist_session: bool,
            local_storage: Dict[str, Any],
            headers: Dict[str, str],
    ) -> RobustaAuthClient:
        """Creates a wrapped instance of the GoTrue Client."""
        return RobustaAuthClient(
            url=auth_url,
            auto_refresh_token=auto_refresh_token,
            detect_session_in_url=detect_session_in_url,
            persist_session=persist_session,
            local_storage=local_storage,
            headers=headers,
        )


class SupabaseDal:
    def __init__(
            self,
            url: str,
            key: str,
            account_id: str,
            email: str,
            password: str,
            sink_name: str,
            cluster_name: str,
            signing_key: str,
    ):
        self.url = url
        self.key = key
        self.account_id = account_id
        self.cluster = cluster_name
        self.client = RobustaClient(url, key)
        self.email = email
        self.password = password
        self.sign_in_time = 0
        self.sign_in()
        self.sink_name = sink_name
        self.signing_key = signing_key

    def __to_db_scanResult(self, scanResult: ScanReportRow) -> Dict[Any, Any]:
        db_sr = scanResult.dict()
        db_sr["account_id"] = self.account_id
        db_sr["cluster_id"] = self.cluster
        return db_sr

    def persist_scan(self, enrichment: Enrichment):
        for block in enrichment.blocks:
            if not isinstance(block, ScanReportBlock):
                continue

            db_scanResults = [self.__to_db_scanResult(sr) for sr in block.results]
            res = self.client.table(SCANS_RESULT_TABLE).insert(db_scanResults).execute()
            if res.get("status_code") not in [200, 201]:
                msg = f"Failed to persist scan {block.scan_id} error: {res.get('data')}"
                logging.error(msg)
                self.handle_supabase_error()
                raise Exception(msg)

            res = self.__rpc_patch(
                "insert_scan_meta",
                {
                    "_account_id": self.account_id,
                    "_cluster": self.cluster,
                    "_scan_id": block.scan_id,
                    "_scan_start": str(block.start_time),
                    "_scan_end": str(block.end_time),
                    "_type": block.type,
                    "_grade": block.score,
                },
            )

            if res.get("status_code") not in [200, 201, 204]:
                msg = f"Failed to persist scan meta {block.scan_id} error: {res.get('data')}"
                logging.error(msg)
                self.handle_supabase_error()
                raise Exception(msg)

    def persist_finding(self, finding: Finding):

        scans, enrichments = [], []
        for enrich in finding.enrichments:
            scans.append(enrich) if enrich.annotations.get(EnrichmentAnnotation.SCAN, False) else enrichments.append(
                enrich
            )

        for scan in scans:
            self.persist_scan(scan)

        if (len(scans) > 0) and (len(enrichments)) == 0:
            return

        for enrichment in enrichments:
            res = (
                self.client.table(EVIDENCE_TABLE)
                .insert(
                    ModelConversion.to_evidence_json(
                        account_id=self.account_id,
                        cluster_id=self.cluster,
                        sink_name=self.sink_name,
                        signing_key=self.signing_key,
                        finding_id=finding.id,
                        enrichment=enrichment,
                    )
                )
                .execute()
            )
            if res.get("status_code") != 201:
                logging.error(
                    f"Failed to persist finding {finding.id} enrichment {enrichment} error: {res.get('data')}"
                )

        res = (
            self.client.table(ISSUES_TABLE)
            .insert(ModelConversion.to_finding_json(self.account_id, self.cluster, finding))
            .execute()
        )
        if res.get("status_code") != 201:
            logging.error(f"Failed to persist finding {finding.id} error: {res.get('data')}")
            self.handle_supabase_error()

    def to_service(self, service: ServiceInfo) -> Dict[Any, Any]:
        return {
            "name": service.name,
            "type": service.service_type,
            "namespace": service.namespace,
            "classification": service.classification,
            "cluster": self.cluster,
            "account_id": self.account_id,
            "deleted": service.deleted,
            "service_key": service.get_service_key(),
            "config": service.service_config.dict() if service.service_config else None,
            "total_pods": service.total_pods,
            "ready_pods": service.ready_pods,
            "update_time": "now()",
        }

    def persist_services(self, services: List[ServiceInfo]):
        if not services:
            return
        db_services = [self.to_service(service) for service in services]
        res = self.client.table(SERVICES_TABLE).insert(db_services, upsert=True).execute()
        if res.get("status_code") not in [200, 201]:
            logging.error(f"Failed to persist services {services} error: {res.get('data')}")
            self.handle_supabase_error()
            status_code = res.get("status_code")
            raise Exception(f"publish service failed. status: {status_code}")

    def get_active_services(self) -> List[ServiceInfo]:
        res = (
            self.client.table(SERVICES_TABLE)
            .select("name", "type", "namespace", "classification", "config", "ready_pods", "total_pods")
            .filter("account_id", "eq", self.account_id)
            .filter("cluster", "eq", self.cluster)
            .filter("deleted", "eq", False)
            .execute()
        )
        if res.get("status_code") not in [200]:
            msg = f"Failed to get existing services (supabase) error: {res.get('data')}"
            logging.error(msg)
            self.handle_supabase_error()
            raise Exception(msg)
        return [
            ServiceInfo(
                name=service["name"],
                service_type=service["type"],
                namespace=service["namespace"],
                classification=service["classification"],
                service_config=service.get("config"),
                ready_pods=service["ready_pods"],
                total_pods=service["total_pods"],
            )
            for service in res.get("data")
        ]

    def has_cluster_findings(self) -> bool:
        res = (
            self.client.table(ISSUES_TABLE)
            .select("*")
            .filter("account_id", "eq", self.account_id)
            .filter("cluster", "eq", self.cluster)
            .limit(1)
            .execute()
        )
        if res.get("status_code") not in [200]:
            msg = f"Failed to check cluster issues: {res.get('data')}"
            logging.error(msg)
            self.handle_supabase_error()
            raise Exception(msg)

        return len(res.get("data")) > 0

    def get_active_nodes(self) -> List[NodeInfo]:
        res = (
            self.client.table(NODES_TABLE)
            .select("*")
            .filter("account_id", "eq", self.account_id)
            .filter("cluster_id", "eq", self.cluster)
            .filter("deleted", "eq", False)
            .execute()
        )
        if res.get("status_code") not in [200]:
            msg = f"Failed to get existing nodes (supabase) error: {res.get('data')}"
            logging.error(msg)
            self.handle_supabase_error()
            raise Exception(msg)

        return [
            NodeInfo(
                name=node["name"],
                node_creation_time=node["node_creation_time"],
                taints=node["taints"],
                conditions=node["conditions"],
                memory_capacity=node["memory_capacity"],
                memory_allocatable=node["memory_allocatable"],
                memory_allocated=node["memory_allocated"],
                cpu_capacity=node["cpu_capacity"],
                cpu_allocatable=node["cpu_allocatable"],
                cpu_allocated=node["cpu_allocated"],
                pods_count=node["pods_count"],
                pods=node["pods"],
                internal_ip=node["internal_ip"],
                external_ip=node["external_ip"],
                node_info=json.loads(node["node_info"]),
            )
            for node in res.get("data")
        ]

    def __to_db_node(self, node: NodeInfo) -> Dict[Any, Any]:
        db_node = node.dict()
        db_node["account_id"] = self.account_id
        db_node["cluster_id"] = self.cluster
        db_node["updated_at"] = "now()"
        return db_node

    def publish_nodes(self, nodes: List[NodeInfo]):
        if not nodes:
            return

        db_nodes = [self.__to_db_node(node) for node in nodes]
        res = self.client.table(NODES_TABLE).insert(db_nodes, upsert=True).execute()
        if res.get("status_code") not in [200, 201]:
            logging.error(f"Failed to persist node {nodes} error: {res.get('data')}")
            self.handle_supabase_error()
            status_code = res.get("status_code")
            raise Exception(f"publish nodes failed. status: {status_code}")

    def get_active_jobs(self) -> List[JobInfo]:
        res = (
            self.client.table(JOBS_TABLE)
            .select("*")
            .filter("account_id", "eq", self.account_id)
            .filter("cluster_id", "eq", self.cluster)
            .filter("deleted", "eq", False)
            .execute()
        )
        if res.get("status_code") not in [200]:
            msg = f"Failed to get existing jobs (supabase) error: {res.get('data')}"
            logging.error(msg)
            self.handle_supabase_error()
            raise Exception(msg)

        return [JobInfo.from_db_row(job) for job in res.get("data")]

    def __to_db_job(self, job: JobInfo) -> Dict[Any, Any]:
        db_job = job.dict()
        db_job["account_id"] = self.account_id
        db_job["cluster_id"] = self.cluster
        db_job["service_key"] = job.get_service_key()
        db_job["updated_at"] = "now()"
        return db_job

    def publish_jobs(self, jobs: List[JobInfo]):
        if not jobs:
            return

        db_jobs = [self.__to_db_job(job) for job in jobs]
        res = self.client.table(JOBS_TABLE).insert(db_jobs, upsert=True).execute()
        if res.get("status_code") not in [200, 201]:
            logging.error(f"Failed to persist jobs {jobs} error: {res.get('data')}")
            self.handle_supabase_error()
            status_code = res.get("status_code")
            raise Exception(f"publish jobs failed. status: {status_code}")

    def remove_deleted_job(self, job: JobInfo):
        if not job:
            return

        res = self.__delete_patch(
            self.client.table(JOBS_TABLE)
            .delete()
            .eq("account_id", self.account_id)
            .eq("cluster_id", self.cluster)
            .eq("service_key", job.get_service_key())
        )
        status_code = res.get("status_code")
        valid_deleted_statuses = [204, 200, 202]
        if status_code not in valid_deleted_statuses:
            logging.error(f"Failed to delete job {job} error: {res.get('data')}")
            self.handle_supabase_error()
            raise Exception(f"remove deleted job failed. status: {status_code}")

    # helm release
    def get_active_helm_release(self) -> List[HelmRelease]:
        res = (
            self.client.table(HELM_RELEASES_TABLE)
            .select("*")
            .filter("account_id", "eq", self.account_id)
            .filter("cluster_id", "eq", self.cluster)
            .filter("deleted", "eq", False)
            .execute()
        )
        if res.get("status_code") not in [200]:
            msg = f"Failed to get existing helm releases (supabase) error: {res.get('data')}"
            logging.error(msg)
            self.handle_supabase_error()
            raise Exception(msg)

        return [HelmRelease.from_db_row(helm_release) for helm_release in res.get("data")]

    def __to_db_helm_release(self, helm_release: HelmRelease) -> Dict[Any, Any]:
        db_helm_release = helm_release.dict()
        db_helm_release["account_id"] = self.account_id
        db_helm_release["cluster_id"] = self.cluster
        db_helm_release["service_key"] = helm_release.get_service_key()
        db_helm_release["updated_at"] = "now()"
        return db_helm_release

    def publish_helm_releases(self, helm_releases: List[HelmRelease]):
        if not helm_releases:
            return

        db_helm_releases = [self.__to_db_helm_release(helm_release) for helm_release in helm_releases]
        logging.debug(f"[supabase] Publishing the helm_releases {db_helm_releases}")

        res = self.client.table(HELM_RELEASES_TABLE).insert(db_helm_releases, upsert=True).execute()
        if res.get("status_code") not in [200, 201]:
            logging.error(f"Failed to persist helm_releases {helm_releases} error: {res.get('data')}")
            self.handle_supabase_error()
            status_code = res.get("status_code")
            raise Exception(f"publish helm_releases failed. status: {status_code}")

    @staticmethod
    def __delete_patch(supabase_request_obj: QueryRequestBuilder) -> Dict[str, Any]:
        """
        supabase_py's QueryBuilder has a bug for delete where the response 204 (no content)
        is attempted to be converted to a json, which throws an error every time
        """
        url: str = str(supabase_request_obj.session.base_url).rstrip("/")

        # postgres_py (which supabase cli uses) adds quotation marks around params with the characters ",.:()"
        # supabase does not support this format
        query: str = str(supabase_request_obj.session.params).replace("%22", "")
        response = requests.delete(f"{url}?{query}", headers=supabase_request_obj.session.headers)
        response_data = ""
        try:
            response_data = response.json()
        except Exception:  # this can be okay if no data is expected
            logging.debug("Failed to parse delete response data")

        return {
            "data": response_data,
            "status_code": response.status_code,
        }

    def __rpc_patch(self, func_name: str, params: dict) -> Dict[str, Any]:
        """
        Supabase client is async. Sync impl of rpc call
        """
        builder = self.client.table(f"rpc/{func_name}")  # rpc builder
        url: str = str(builder.session.base_url).rstrip("/")

        response = requests.post(url, headers=builder.session.headers, json=params)
        response_data = {}
        try:
            if response.content:
                response_data = response.json()
        except Exception:  # this can be okay if no data is expected
            logging.debug("Failed to parse delete response data")

        return {
            "data": response_data,
            "status_code": response.status_code,
        }

    def sign_in(self):
        if time.time() > self.sign_in_time + SUPABASE_LOGIN_RATE_LIMIT_SEC:
            logging.info("Supabase dal login")
            self.sign_in_time = time.time()
            self.client.auth.sign_in(email=self.email, password=self.password)

    def handle_supabase_error(self):
        """Workaround for Gotrue bug in refresh token."""
        # If there's an error during refresh token, no new refresh timer task is created, and the client remains not authenticated for good
        # When there's an error connecting to supabase server, we will re-login, to re-authenticate the session.
        # Adding rate-limiting mechanism, not to login too much because of other errors
        # https://github.com/supabase/gotrue-py/issues/9
        try:
            self.sign_in()
        except Exception:
            logging.error("Failed to sign in on error", exc_info=True)

    def to_db_cluster_status(self, data: ClusterStatus) -> Dict[str, Any]:
        db_cluster_status = data.dict()
        if data.last_alert_at is None:
            del db_cluster_status["last_alert_at"]

        db_cluster_status["updated_at"] = "now()"

        log_cluster_status = db_cluster_status.copy()
        log_cluster_status["light_actions"] = len(data.light_actions)
        logging.info(f"cluster status {log_cluster_status}")

        return db_cluster_status

    def publish_cluster_status(self, cluster_status: ClusterStatus):
        res = (
            self.client.table(CLUSTERS_STATUS_TABLE)
            .insert(self.to_db_cluster_status(cluster_status), upsert=True)
            .execute()
        )
        if res.get("status_code") not in [200, 201]:
            logging.error(f"Failed to upsert {self.to_db_cluster_status(cluster_status)} error: {res.get('data')}")
            self.handle_supabase_error()

    def get_active_namespaces(self) -> List[NamespaceInfo]:
        res = (
            self.client.table(NAMESPACES_TABLE)
            .select("*")
            .filter("account_id", "eq", self.account_id)
            .filter("cluster_id", "eq", self.cluster)
            .filter("deleted", "eq", False)
            .execute()
        )
        if res.get("status_code") not in [200]:
            msg = f"Failed to get existing namespaces (supabase) error: {res.get('data')}"
            logging.error(msg)
            self.handle_supabase_error()
            raise Exception(f"get active namespaces failed. status: {res.get('status_code')}")

        return [NamespaceInfo.from_db_row(namespace) for namespace in res.get("data")]

    def __to_db_namespace(self, namespace: NamespaceInfo) -> Dict[Any, Any]:
        db_job = namespace.dict()
        db_job["account_id"] = self.account_id
        db_job["cluster_id"] = self.cluster
        db_job["updated_at"] = "now()"
        return db_job

    def publish_namespaces(self, namespaces: List[NamespaceInfo]):
        if not namespaces:
            return

        db_namespaces = [self.__to_db_namespace(namespace) for namespace in namespaces]
        res = self.client.table(NAMESPACES_TABLE).insert(db_namespaces, upsert=True).execute()
        if res.get("status_code") not in [200, 201]:
            logging.error(f"Failed to persist namespaces {namespaces} error: {res.get('data')}")
            self.handle_supabase_error()
            status_code = res.get("status_code")
            raise Exception(f"publish namespaces failed. status: {status_code}")

    def publish_cluster_nodes(self, node_count: int):
        data = {
            "_account_id": self.account_id,
            "_cluster_id": self.cluster,
            "_node_count": node_count,
        }
        res = self.__rpc_patch(UPDATE_CLUSTER_NODE_COUNT, data)

        if res.get("status_code") not in [200, 201, 204]:
            logging.error(f"Failed to publish node count {data} error: {res.get('data')}")
            self.handle_supabase_error()

        logging.info(f"cluster nodes: {UPDATE_CLUSTER_NODE_COUNT} => {data}")
