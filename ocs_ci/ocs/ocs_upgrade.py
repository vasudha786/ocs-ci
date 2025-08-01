import os
import logging
from copy import deepcopy
from pkg_resources import parse_version
from tempfile import NamedTemporaryFile
import time

from selenium.webdriver.common.by import By
from ocs_ci.framework import config
from ocs_ci.framework.logger_helper import log_step
from ocs_ci.helpers.helpers import verify_nb_db_psql_version
from ocs_ci.deployment.deployment import (
    create_catalog_source,
    create_ocs_secret,
    Deployment,
)
from ocs_ci.utility.deployment import get_and_apply_idms_from_catalog
from ocs_ci.deployment.disconnected import prepare_disconnected_ocs_deployment
from ocs_ci.deployment.helpers.external_cluster_helpers import (
    ExternalCluster,
    get_external_cluster_client,
)
from ocs_ci.deployment.helpers.odf_deployment_helpers import (
    get_required_csvs,
    set_ceph_config,
)
from ocs_ci.ocs import constants
from ocs_ci.ocs.cluster import CephCluster, CephHealthMonitor
from ocs_ci.ocs.defaults import (
    EXTERNAL_CLUSTER_USER,
    MUST_GATHER_UPSTREAM_IMAGE,
    MUST_GATHER_UPSTREAM_TAG,
    OCS_OPERATOR_NAME,
)
from ocs_ci.ocs.ocp import get_images, OCP
from ocs_ci.ocs.node import get_nodes
from ocs_ci.ocs.resources.catalog_source import CatalogSource, disable_specific_source
from ocs_ci.ocs.resources.daemonset import DaemonSet
from ocs_ci.ocs.resources.csv import (
    CSV,
    check_all_csvs_are_succeeded,
    get_csvs_start_with_prefix,
)
from ocs_ci.ocs.resources.install_plan import wait_for_install_plan_and_approve
from ocs_ci.ocs.resources.pod import (
    get_noobaa_pods,
    verify_pods_upgraded,
)
from ocs_ci.ocs.resources.packagemanifest import (
    get_selector_for_ocs_operator,
    PackageManifest,
)
from ocs_ci.ocs.resources.storage_cluster import (
    get_osd_count,
    mcg_only_install_verification,
    ocs_install_verification,
)
from ocs_ci.ocs.utils import setup_ceph_toolbox, get_expected_nb_db_psql_version
from ocs_ci.utility import version
from ocs_ci.utility.reporting import update_live_must_gather_image
from ocs_ci.utility.retry import retry
from ocs_ci.utility.rgwutils import get_rgw_count
from ocs_ci.utility.utils import (
    decode,
    exec_cmd,
    get_latest_ds_olm_tag,
    get_next_version_available_for_upgrade,
    get_ocs_version_from_image,
    load_config_file,
    TimeoutSampler,
)
from ocs_ci.utility.secret import link_all_sa_and_secret_and_delete_pods
from ocs_ci.utility.templating import dump_data_to_temp_yaml
from ocs_ci.ocs.exceptions import (
    TimeoutException,
    ExternalClusterRGWAdminOpsUserException,
    CSVNotFound,
)
from ocs_ci.ocs.ui.base_ui import logger, login_ui
from ocs_ci.ocs.ui.views import locators, ODF_OPERATOR
from ocs_ci.utility.utils import get_ocp_version
from ocs_ci.ocs.ui.deployment_ui import DeploymentUI
from ocs_ci.ocs.ui.validation_ui import ValidationUI
from ocs_ci.utility.ibmcloud import run_ibmcloud_cmd

log = logging.getLogger(__name__)


def get_upgrade_image_info(old_csv_images, new_csv_images):
    """
    Log the info about the images which are going to be upgraded.

    Args:
        old_csv_images (dict): dictionary with the old images from pre upgrade CSV
        new_csv_images (dict): dictionary with the post upgrade images

    Returns:
        tuple: with three sets which contain those types of images
            old_images_for_upgrade - images which should be upgraded
            new_images_to_upgrade - new images to which upgrade
            unchanged_images - unchanged images without upgrade

    """
    old_csv_images = set(old_csv_images.values())
    new_csv_images = set(new_csv_images.values())
    # Ignore the same SHA images for BZ:
    # https://bugzilla.redhat.com/show_bug.cgi?id=1994007
    for old_image in old_csv_images.copy():
        old_image_sha = None
        if constants.SHA_SEPARATOR in old_image:
            _, old_image_sha = old_image.split(constants.SHA_SEPARATOR)
        if old_image_sha:
            for new_image in new_csv_images:
                if old_image_sha in new_image:
                    log.info(
                        f"There is a new image: {new_image} with the same SHA "
                        f"which is the same as the old image: {old_image}. "
                        "This image will be ignored because of this BZ: 1994007"
                    )
                    old_csv_images.remove(old_image)
    old_images_for_upgrade = old_csv_images - new_csv_images
    log.info(
        f"Old images which are going to be upgraded: "
        f"{sorted(old_images_for_upgrade)}"
    )
    new_images_to_upgrade = new_csv_images - old_csv_images
    log.info(f"New images for upgrade: " f"{sorted(new_images_to_upgrade)}")
    unchanged_images = old_csv_images.intersection(new_csv_images)
    log.info(f"Unchanged images after upgrade: " f"{sorted(unchanged_images)}")
    return (
        old_images_for_upgrade,
        new_images_to_upgrade,
        unchanged_images,
    )


@retry(ValueError, tries=20, delay=30, backoff=1)
def get_expected_noobaa_pod_count(upgrade_version):
    """
    Verify if all the images of OCS objects got upgraded

    Args:
        upgrade_version (packaging.version.Version): version of OCS

    Returns:
        int: number of expected noobaa pods

    """
    expected_noobaa_pods = [
        "noobaa-core-0",
        "noobaa-operator",
        "noobaa-db-pg-cluster-1",
        "noobaa-db-pg-cluster-2",
    ]

    endpoint_count = 0
    noobaa_pod_obj = get_noobaa_pods()
    noobaa_pod_names = [pod.name for pod in noobaa_pod_obj]
    logger.info(f"Current noobaa pods under validation: {noobaa_pod_names}")
    for pod in noobaa_pod_obj:
        if "pv-backingstore" in pod.name:
            expected_noobaa_pods.append(pod.name)
        if upgrade_version >= parse_version("4.19"):
            if "noobaa-default-backing-store" in pod.name:
                logger.info(
                    "In some cases like MCG only or HCI we are counting noobaa-default-backing-store"
                )
                expected_noobaa_pods.append(pod.name)
            if "cnpg-controller-manager" in pod.name:
                expected_noobaa_pods.append(pod.name)
        if "noobaa-endpoint" in pod.name:
            endpoint_count += 1
            expected_noobaa_pods.append(pod.name)

    noobaa = OCP(kind="noobaa", namespace=config.ENV_DATA["cluster_namespace"])
    resource = noobaa.get()["items"][0]
    endpoints = resource.get("spec", {}).get("endpoints", {})
    max_endpoints = endpoints.get("maxCount", constants.MAX_NB_ENDPOINT_COUNT)
    min_endpoints = endpoints.get(
        "minCount", constants.MIN_NB_ENDPOINT_COUNT_POST_DEPLOYMENT
    )

    if not (min_endpoints <= endpoint_count <= max_endpoints):
        raise ValueError(
            f"Endpoint pod count {endpoint_count} not in allowed range [{min_endpoints}, {max_endpoints}]"
        )

    if len(noobaa_pod_obj) != len(expected_noobaa_pods):
        raise ValueError(
            f"Expected noobaa pods: {expected_noobaa_pods} do not match actual noobaa pods: {noobaa_pod_names}"
        )
    return len(noobaa_pod_obj)


@retry(Exception, tries=3, delay=60, backoff=1)
def verify_noobaa_pods_upgraded(old_images, upgrade_version):
    """
    Verify noobaa pods are upgraded

    Args:
        old_images (set): set with old images
        upgrade_version (packaging.version.Version): version of OCS

    """
    noobaa_pods = get_expected_noobaa_pod_count(upgrade_version)
    noobaa_db_psql_version = get_expected_nb_db_psql_version()
    ignore_psql_12_verification = (
        int(noobaa_db_psql_version) != constants.NOOBAA_POSTGRES_12_VERSION
    )

    num_nodes = (
        config.ENV_DATA["worker_replicas"]
        + config.ENV_DATA["master_replicas"]
        + config.ENV_DATA.get("infra_replicas", 0)
    )
    timeout = 600 if num_nodes < 6 else 900

    verify_pods_upgraded(
        old_images,
        selector=constants.NOOBAA_APP_LABEL,
        count=noobaa_pods,
        timeout=timeout,
        ignore_psql_12_verification=ignore_psql_12_verification,
    )


def verify_image_versions(old_images, upgrade_version, version_before_upgrade):
    """
    Verify if all the images of OCS objects got upgraded

    Args:
        old_images (set): set with old images
        upgrade_version (packaging.version.Version): version of OCS
        version_before_upgrade (float): version of OCS before upgrade

    """
    number_of_worker_nodes = len(get_nodes())
    verify_pods_upgraded(old_images, selector=constants.OCS_OPERATOR_LABEL)
    verify_pods_upgraded(old_images, selector=constants.OPERATOR_LABEL)
    verify_noobaa_pods_upgraded(old_images, upgrade_version)
    if not config.ENV_DATA.get("mcg_only_deployment"):
        odf_running_version = version.get_ocs_version_from_csv(only_major_minor=True)
        # cephfs and rbdplugin label and count
        csi_cephfsplugin_label = constants.CSI_CEPHFSPLUGIN_LABEL
        csi_rbdplugin_label = constants.CSI_RBDPLUGIN_LABEL
        csi_cephfsplugin_provisioner_label = (
            constants.CSI_CEPHFSPLUGIN_PROVISIONER_LABEL
        )
        csi_rbdplugin_provisioner_label = constants.CSI_RBDPLUGIN_PROVISIONER_LABEL
        count_csi_cephfsplugin_label = count_csi_rbdplugin_label = (
            number_of_worker_nodes
        )
        if odf_running_version >= version.VERSION_4_19:
            csi_cephfsplugin_label = constants.CSI_CEPHFSPLUGIN_LABEL_419
            csi_rbdplugin_label = constants.CSI_RBDPLUGIN_LABEL_419
            csi_cephfsplugin_provisioner_label = (
                constants.CSI_CEPHFSPLUGIN_PROVISIONER_LABEL_419
            )
            csi_rbdplugin_provisioner_label = (
                constants.CSI_RBDPLUGIN_PROVISIONER_LABEL_419
            )
            count_csi_cephfsplugin_label = count_csi_rbdplugin_label = (
                number_of_worker_nodes
            )
        else:
            log.info(
                f"Label for cephfsplugin and rbdplugin are {csi_cephfsplugin_label} and {csi_rbdplugin_label}"
            )
        verify_pods_upgraded(
            old_images,
            selector=csi_cephfsplugin_label,
            count=count_csi_cephfsplugin_label,
        )
        verify_pods_upgraded(
            old_images, selector=csi_cephfsplugin_provisioner_label, count=2
        )
        verify_pods_upgraded(
            old_images,
            selector=csi_rbdplugin_label,
            count=count_csi_rbdplugin_label,
        )
        verify_pods_upgraded(
            old_images, selector=csi_rbdplugin_provisioner_label, count=2
        )
    if not (
        config.DEPLOYMENT.get("external_mode")
        or config.ENV_DATA.get("mcg_only_deployment")
    ):
        mon_count = 3
        if config.DEPLOYMENT.get("arbiter_deployment"):
            mon_count = 5
        verify_pods_upgraded(
            old_images,
            selector=constants.MON_APP_LABEL,
            count=mon_count,
            timeout=820,
        )
        mgr_count = constants.MGR_COUNT_415
        if upgrade_version < parse_version("4.15"):
            mgr_count = constants.MGR_COUNT
        verify_pods_upgraded(
            old_images, selector=constants.MGR_APP_LABEL, count=mgr_count
        )
        osd_timeout = 600 if upgrade_version >= parse_version("4.5") else 750
        osd_count = get_osd_count()
        # In the debugging issue:
        # https://github.com/red-hat-storage/ocs-ci/issues/5031
        # Noticed that it's taking about 1 more minute from previous check till actual
        # OSD pods getting restarted.
        # Hence adding sleep here for 120 seconds to be sure, OSD pods upgrade started.
        log.info("Waiting for 2 minutes before start checking OSD pods")
        time.sleep(120)
        verify_pods_upgraded(
            old_images,
            selector=constants.OSD_APP_LABEL,
            count=osd_count,
            timeout=osd_timeout * osd_count,
        )
        verify_pods_upgraded(old_images, selector=constants.MDS_APP_LABEL, count=2)
        if config.ENV_DATA.get("platform") in constants.ON_PREM_PLATFORMS:
            rgw_count = get_rgw_count(
                upgrade_version.base_version, True, version_before_upgrade
            )
            verify_pods_upgraded(
                old_images,
                selector=constants.RGW_APP_LABEL,
                count=rgw_count,
            )
    if upgrade_version >= parse_version("4.6"):
        verify_pods_upgraded(old_images, selector=constants.OCS_METRICS_EXPORTER)


class OCSUpgrade(object):
    """
    OCS Upgrade helper class

    """

    def __init__(
        self,
        namespace,
        version_before_upgrade,
        ocs_registry_image,
        upgrade_in_current_source,
    ):
        self.namespace = namespace
        self._version_before_upgrade = version_before_upgrade
        self._ocs_registry_image = ocs_registry_image
        self.upgrade_in_current_source = upgrade_in_current_source
        self.subscription_plan_approval = config.DEPLOYMENT.get(
            "subscription_plan_approval"
        )
        self.ocp_version = get_ocp_version()
        if config.UPGRADE.get("ui_upgrade"):
            self.validation_loc = locators[self.ocp_version]["validation"]

    @property
    def version_before_upgrade(self):
        return self._version_before_upgrade

    @property
    def ocs_registry_image(self):
        return self._ocs_registry_image

    @ocs_registry_image.setter
    def ocs_registry_image(self, value):
        self._ocs_registry_image = value

    def get_upgrade_version(self):
        """
        Getting the required upgrade version

        Returns:
            str: version to be upgraded

        """
        upgrade_version = config.UPGRADE.get(
            "upgrade_ocs_version", self.version_before_upgrade
        )
        if self.ocs_registry_image:
            upgrade_version = get_ocs_version_from_image(self.ocs_registry_image)

        return upgrade_version

    def get_parsed_versions(self):
        """
        Getting parsed version names, current version, and upgrade version

        Returns:
            tuple: contains 2 strings with parsed names of current version
                and upgrade version

        """
        parsed_version_before_upgrade = parse_version(self.version_before_upgrade)
        parsed_upgrade_version = parse_version(self.get_upgrade_version())

        return parsed_version_before_upgrade, parsed_upgrade_version

    def load_version_config_file(self, upgrade_version):
        """
        Loads config file to the ocs-ci config with upgrade version

        Args:
            upgrade_version: (str): version to be upgraded

        """

        live_deployment = config.DEPLOYMENT["live_deployment"]
        upgrade_in_same_source = config.UPGRADE.get("upgrade_in_current_source", False)
        version_change = self.get_parsed_versions()[1] > self.get_parsed_versions()[0]
        # When upgrading to internal build of same version, we usually deploy from GAed (live) version.
        # In this case, we need to reload config to get internal must-gather image back to default.
        reload_config = (
            not version_change and live_deployment and not upgrade_in_same_source
        )
        if version_change or reload_config:
            version_config_file = os.path.join(
                constants.OCS_VERSION_CONF_DIR, f"ocs-{upgrade_version}.yaml"
            )
            log.info(f"Reloading config file for OCS/ODF version: {upgrade_version}.")
            load_config_file(version_config_file)
        else:
            log.info(
                f"Upgrade version {upgrade_version} is not higher than old version:"
                f" {self.version_before_upgrade}, config file will not be loaded"
            )
        # For IBM ROKS cloud, there is no possibility to use internal build of must gather image.
        # If we are not testing the live upgrade, then we will need to change images to the upsream.
        managed_ibmcloud_platform = (
            config.ENV_DATA["platform"] == constants.IBMCLOUD_PLATFORM
            and config.ENV_DATA["deployment_type"] == "managed"
        )
        use_upstream_mg_image = managed_ibmcloud_platform and not upgrade_in_same_source
        if (
            (live_deployment and upgrade_in_same_source)
            or (managed_ibmcloud_platform and not use_upstream_mg_image)
            or config.ENV_DATA.get("platform") == constants.ROSA_HCP_PLATFORM
        ):
            update_live_must_gather_image()
        elif use_upstream_mg_image:
            config.REPORTING["ocs_must_gather_image"] = MUST_GATHER_UPSTREAM_IMAGE
            config.REPORTING["ocs_must_gather_latest_tag"] = MUST_GATHER_UPSTREAM_TAG
        else:
            must_gather_image = config.REPORTING["default_ocs_must_gather_image"]
            must_gather_tag = config.REPORTING["default_ocs_must_gather_latest_tag"]
            log.info(
                f"Reloading to default must gather image: {must_gather_image}:{must_gather_tag}"
            )
            config.REPORTING["ocs_must_gather_image"] = must_gather_image
            config.REPORTING["ocs_must_gather_latest_tag"] = must_gather_tag

    def get_csv_name_pre_upgrade(self, resource_name=OCS_OPERATOR_NAME):
        """
        Get pre-upgrade CSV name

        Ealier we used to depend on packagemanifest to find the pre-upgrade
        csv name. Due to issues in catalogsource where csv names were not shown properly once
        catalogsource for upgrade version has been created, we are taking new approach of
        finding csv name from csv list and also look for pre-upgrade ocs version for finding out
        the actual csv

        """
        csv_name = None
        csv_list = get_csvs_start_with_prefix(resource_name, namespace=self.namespace)
        for csv in csv_list:
            if resource_name in csv.get("metadata").get("name"):
                if config.PREUPGRADE_CONFIG.get("ENV_DATA").get(
                    "ocs_version"
                ) in csv.get("metadata").get("name"):
                    csv_name = csv.get("metadata").get("name")
                    return csv_name
        raise CSVNotFound(f"No preupgrade CSV found for {resource_name}")

    def get_pre_upgrade_image(self, csv_name_pre_upgrade):
        """
        Getting all OCS cluster images, before upgrade

        Args:
            csv_name_pre_upgrade: (str): OCS operator name

        Returns:
            dict: Contains all OCS cluster images
                dict keys: Image name
                dict values: Image full path

        """
        csv_name_pre_upgrade = csv_name_pre_upgrade
        log.info(f"CSV name before upgrade is: {csv_name_pre_upgrade}")
        csv_pre_upgrade = CSV(
            resource_name=csv_name_pre_upgrade, namespace=self.namespace
        )
        return get_images(csv_pre_upgrade.get())

    def set_upgrade_channel(self, resource_name=OCS_OPERATOR_NAME):
        """
        Wait for the new package manifest for upgrade.

        Returns:
            str: OCS subscription channel

        """
        operator_selector = get_selector_for_ocs_operator()
        package_manifest = PackageManifest(
            resource_name=resource_name,
            selector=operator_selector,
        )
        package_manifest.wait_for_resource(timeout=120)
        channel = config.DEPLOYMENT.get("ocs_csv_channel")
        if not channel:
            channel = package_manifest.get_default_channel()

        return channel

    def update_subscription(self, channel):
        """
        Updating OCS operator subscription

        Args:
            channel: (str): OCS subscription channel

        """
        if version.get_semantic_ocs_version_from_config() >= version.VERSION_4_9:
            subscription_name = constants.ODF_SUBSCRIPTION
        else:
            subscription_name = constants.OCS_SUBSCRIPTION
        kind_name = "subscription.operators.coreos.com"
        subscription = OCP(
            resource_name=subscription_name,
            kind=kind_name,
            namespace=config.ENV_DATA["cluster_namespace"],
        )
        current_ocs_source = subscription.data["spec"]["source"]
        log.info(f"Current OCS subscription source: {current_ocs_source}")
        ocs_source = (
            current_ocs_source
            if self.upgrade_in_current_source
            else constants.OPERATOR_CATALOG_SOURCE_NAME
        )
        patch_subscription_cmd = (
            f"patch {kind_name} {subscription_name} "
            f'-n {self.namespace} --type merge -p \'{{"spec":{{"channel": '
            f'"{channel}", "source": "{ocs_source}"}}}}\''
        )
        subscription.exec_oc_cmd(patch_subscription_cmd, out_yaml_format=False)

    def check_if_upgrade_completed(self, channel, csv_name_pre_upgrade):
        """
        Checks if OCS operator finishes it's upgrade

        Args:
            channel: (str): OCS subscription channel
            csv_name_pre_upgrade: (str): OCS operator name

        Returns:
            bool: True if upgrade completed, False otherwise

        """
        if not check_all_csvs_are_succeeded(self.namespace):
            log.warning("One of CSV is still not upgraded!")
            return False
        operator_selector = get_selector_for_ocs_operator()
        package_manifest = PackageManifest(
            resource_name=OCS_OPERATOR_NAME,
            selector=operator_selector,
            subscription_plan_approval=self.subscription_plan_approval,
        )
        csv_name_post_upgrade = package_manifest.get_current_csv(channel)
        if csv_name_post_upgrade == csv_name_pre_upgrade:
            log.info(f"CSV is still: {csv_name_post_upgrade}")
            return False
        else:
            log.info(f"CSV now upgraded to: {csv_name_post_upgrade}")
            return True

    def get_images_post_upgrade(
        self,
        channel,
        pre_upgrade_images,
        upgrade_version,
        resource_name=OCS_OPERATOR_NAME,
    ):
        """
        Checks if all images of OCS cluster upgraded,
            and return list of all images if upgrade success

        Args:
            channel: (str): OCS subscription channel
            pre_upgrade_images: (dict): Contains all OCS cluster images
            upgrade_version: (str): version to be upgraded

        Returns:
            set: Contains full path of OCS cluster old images

        """
        operator_selector = get_selector_for_ocs_operator()
        package_manifest = PackageManifest(
            resource_name=resource_name,
            selector=operator_selector,
            subscription_plan_approval=self.subscription_plan_approval,
        )
        csv_name_post_upgrade = package_manifest.get_current_csv(channel)
        csv_post_upgrade = CSV(
            resource_name=csv_name_post_upgrade, namespace=self.namespace
        )
        log.info(f"Waiting for CSV {csv_name_post_upgrade} to be in succeeded state")

        # Workaround for patching missing ceph-rook-tools pod after upgrade
        if self.version_before_upgrade == "4.2" and upgrade_version == "4.3":
            log.info("Force creating Ceph toolbox after upgrade 4.2 -> 4.3")
            setup_ceph_toolbox(force_setup=True)
        # End of workaround

        if config.DEPLOYMENT.get("external_mode") or config.ENV_DATA.get(
            "mcg_only_deployment"
        ):
            timeout = 200
        else:
            timeout = 200 * get_osd_count()
        csv_post_upgrade.wait_for_phase("Succeeded", timeout=timeout)

        post_upgrade_images = get_images(csv_post_upgrade.get())
        old_images, _, _ = get_upgrade_image_info(
            pre_upgrade_images, post_upgrade_images
        )

        return old_images

    def set_upgrade_images(self):
        """
        Set images for upgrade

        """
        ocs_catalog = CatalogSource(
            resource_name=constants.OPERATOR_CATALOG_SOURCE_NAME,
            namespace=constants.MARKETPLACE_NAMESPACE,
        )
        stage_testing = config.DEPLOYMENT.get("stage_rh_osbs")
        konflux_build = config.DEPLOYMENT.get("konflux_build")
        if konflux_build and stage_testing:
            log_step("Creating stage TagMirrorSet")
            exec_cmd(f"oc apply -f {constants.STAGE_TAG_MIRROR_SET_YAML}")
            log_step("Creating stage ImageDigestMirrorSet")
            exec_cmd(f"oc apply -f {constants.STAGE_IMAGE_DIGEST_MIRROR_SET_YAML}")
            log_step("Sleeping 60 seconds after applying tag mirror set.")
            time.sleep(60)
            log_step("Waiting max 30 mins for master MCP to get updated")
            exec_cmd(
                "oc wait --for=condition=Updated --timeout=30m mcp/master", timeout=2100
            )
            log_step("Waiting max 30 mins for worker MCP to get updated")
            exec_cmd(
                "oc wait --for=condition=Updated --timeout=30m mcp/worker", timeout=2100
            )
            return
        if not self.upgrade_in_current_source:
            disable_specific_source(constants.OPERATOR_CATALOG_SOURCE_NAME)
            if not ocs_catalog.is_exist():
                log.info("OCS catalog source doesn't exist. Creating new one.")
                create_catalog_source(self.ocs_registry_image, ignore_upgrade=True)
                # We can return here as new CatalogSource contains right images
                return
            image_url = ocs_catalog.get_image_url()
            image_tag = ocs_catalog.get_image_name()
            log.info(f"Current image is: {image_url}, tag: {image_tag}")
            version_change = (
                self.get_parsed_versions()[1] > self.get_parsed_versions()[0]
            )
            if self.ocs_registry_image:
                image_url, new_image_tag = self.ocs_registry_image.rsplit(":", 1)
            elif config.UPGRADE.get("upgrade_to_latest", True) or version_change:
                new_image_tag = get_latest_ds_olm_tag()
            else:
                new_image_tag = get_next_version_available_for_upgrade(image_tag)
            cs_data = deepcopy(ocs_catalog.data)
            image_for_upgrade = ":".join([image_url, new_image_tag])
            log.info(f"Image: {image_for_upgrade} will be used for upgrade.")
            cs_data["spec"]["image"] = image_for_upgrade

            with NamedTemporaryFile() as cs_yaml:
                dump_data_to_temp_yaml(cs_data, cs_yaml.name)
                ocs_catalog.apply(cs_yaml.name)
                if not config.DEPLOYMENT.get("disconnected"):
                    # on Disconnected cluster, ICSP from the ocs-registry image is not needed/valid
                    get_and_apply_idms_from_catalog(f"{image_url}:{new_image_tag}")


def run_ocs_upgrade(
    operation=None,
    upgrade_stats=None,
    *operation_args,
    **operation_kwargs,
):
    """
    Run upgrade procedure of OCS cluster

    Args:
        operation: (function): Function to run
        upgrade_stats: (dict): Dictionary where can be stored statistics
            gathered during the upgrade
        operation_args: (iterable): Function's arguments
        operation_kwargs: (map): Function's keyword arguments

    """
    namespace = config.ENV_DATA["cluster_namespace"]
    platform = config.ENV_DATA["platform"]
    ceph_cluster = CephCluster()
    original_ocs_version = config.ENV_DATA.get("ocs_version")
    upgrade_in_current_source = config.UPGRADE.get("upgrade_in_current_source", False)
    upgrade_ocs = OCSUpgrade(
        namespace=config.ENV_DATA["cluster_namespace"],
        version_before_upgrade=original_ocs_version,
        ocs_registry_image=config.UPGRADE.get("upgrade_ocs_registry_image"),
        upgrade_in_current_source=upgrade_in_current_source,
    )
    upgrade_version = upgrade_ocs.get_upgrade_version()
    assert (
        upgrade_ocs.get_parsed_versions()[1] >= upgrade_ocs.get_parsed_versions()[0]
    ), (
        f"Version you would like to upgrade to: {upgrade_version} "
        f"is not higher or equal to the version you currently running: "
        f"{upgrade_ocs.version_before_upgrade}"
    )

    # Update values CSI_RBD_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE and CSI_CEPHFS_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE
    # in rook-ceph-operator-config configmap
    set_update_strategy()
    if upgrade_stats:
        cephfs_daemonset = DaemonSet(
            resource_name="csi-cephfsplugin",
            namespace=config.ENV_DATA["cluster_namespace"],
        )
        rbd_daemonset = DaemonSet(
            resource_name="csi-rbdplugin",
            namespace=config.ENV_DATA["cluster_namespace"],
        )
        upgrade_stats["odf_upgrade"]["rbd_max_unavailable"] = 0
        upgrade_stats["odf_upgrade"]["cephfs_max_unavailable"] = 0

    # create external cluster object
    if config.DEPLOYMENT["external_mode"]:
        host, user, password, ssh_key = get_external_cluster_client()
        external_cluster = ExternalCluster(host, user, password, ssh_key)

    # For external cluster , create the secrets if upgraded version is 4.8
    if (
        config.DEPLOYMENT["external_mode"]
        and original_ocs_version == "4.7"
        and upgrade_version == "4.8"
    ):
        external_cluster.create_object_store_user()
        access_key = config.EXTERNAL_MODE.get("access_key_rgw-admin-ops-user", "")
        secret_key = config.EXTERNAL_MODE.get("secret_key_rgw-admin-ops-user", "")
        if not (access_key and secret_key):
            raise ExternalClusterRGWAdminOpsUserException(
                "Access and secret key for rgw-admin-ops-user not found"
            )
        cmd = (
            f'oc create secret generic --type="kubernetes.io/rook"'
            f' "rgw-admin-ops-user" --from-literal=accessKey={access_key} --from-literal=secretKey={secret_key}'
        )
        exec_cmd(cmd)

    csv_name_pre_upgrade = upgrade_ocs.get_csv_name_pre_upgrade()
    pre_upgrade_images = upgrade_ocs.get_pre_upgrade_image(csv_name_pre_upgrade)
    upgrade_ocs.load_version_config_file(upgrade_version)
    start_time = time.time()
    if config.DEPLOYMENT.get("disconnected") and not config.DEPLOYMENT.get(
        "disconnected_env_skip_image_mirroring"
    ):
        upgrade_ocs.ocs_registry_image = prepare_disconnected_ocs_deployment(
            upgrade=True
        )
        log.info(f"Disconnected upgrade - new image: {upgrade_ocs.ocs_registry_image}")

    with CephHealthMonitor(ceph_cluster):
        channel = upgrade_ocs.set_upgrade_channel()
        upgrade_ocs.set_upgrade_images()
        live_deployment = config.DEPLOYMENT["live_deployment"]
        disable_addon = config.DEPLOYMENT.get("ibmcloud_disable_addon")
        managed_ibmcloud_platform = (
            config.ENV_DATA["platform"] == constants.IBMCLOUD_PLATFORM
            and config.ENV_DATA["deployment_type"] == "managed"
        )
        if managed_ibmcloud_platform and live_deployment and not disable_addon:
            clustername = config.ENV_DATA.get("cluster_name")
            cmd = f"ibmcloud ks cluster addon disable openshift-data-foundation --cluster {clustername} -f"
            run_ibmcloud_cmd(cmd)
            time.sleep(120)
            cmd = (
                f"ibmcloud ks cluster addon enable openshift-data-foundation --cluster {clustername} -f --version "
                f"{upgrade_version}.0 --param ocsUpgrade=true"
            )
            run_ibmcloud_cmd(cmd)
            time.sleep(120)
        else:
            ui_upgrade_supported = False
            if config.UPGRADE.get("ui_upgrade"):
                if (
                    version.get_semantic_ocp_version_from_config()
                    == version.VERSION_4_9
                    and original_ocs_version == "4.8"
                    and upgrade_version == "4.9"
                ):
                    ui_upgrade_supported = True
                else:
                    log.warning(
                        "UI upgrade combination is not supported. It will fallback to CLI upgrade"
                    )
            if ui_upgrade_supported:
                ocs_odf_upgrade_ui()
            else:
                if managed_ibmcloud_platform and not upgrade_in_current_source:
                    create_ocs_secret(config.ENV_DATA["cluster_namespace"])
                if upgrade_version != "4.9":
                    # In the case of upgrade to ODF 4.9, the ODF operator should upgrade
                    # OCS automatically.
                    upgrade_ocs.update_subscription(channel)
                if original_ocs_version == "4.8" and upgrade_version == "4.9":
                    deployment = Deployment()
                    deployment.subscribe_ocs()
                else:
                    # In the case upgrade is not from 4.8 to 4.9 and we have manual approval strategy
                    # we need to wait and approve install plan, otherwise it's approved in the
                    # subscribe_ocs method.
                    subscription_plan_approval = config.DEPLOYMENT.get(
                        "subscription_plan_approval"
                    )
                    if subscription_plan_approval == "Manual":
                        wait_for_install_plan_and_approve(
                            config.ENV_DATA["cluster_namespace"]
                        )
                if managed_ibmcloud_platform and not upgrade_in_current_source:
                    for attempt in range(2):
                        # We need to do it twice, because some of the SA are updated
                        # after the first load of OCS pod after upgrade. So we need to
                        # link updated SA again.
                        log.info(
                            f"Sleep 1 minute before attempt: {attempt + 1}/2 "
                            "of linking secret/SAs"
                        )
                        time.sleep(60)
                        link_all_sa_and_secret_and_delete_pods(
                            constants.OCS_SECRET, config.ENV_DATA["cluster_namespace"]
                        )
        if operation:
            log.info(f"Calling test function: {operation}")
            _ = operation(*operation_args, **operation_kwargs)
            # Workaround for issue #2531
            time.sleep(30)
            # End of workaround

        for sample in TimeoutSampler(
            timeout=725,
            sleep=5,
            func=upgrade_ocs.check_if_upgrade_completed,
            channel=channel,
            csv_name_pre_upgrade=csv_name_pre_upgrade,
        ):
            if upgrade_stats:
                rbd_daemonset_status = rbd_daemonset.get_status()
                cephfs_daemonset_status = cephfs_daemonset.get_status()
                rbd_unavailable = (
                    rbd_daemonset_status["desiredNumberScheduled"]
                    - rbd_daemonset_status["numberReady"]
                )
                cephfs_unavailable = (
                    cephfs_daemonset_status["desiredNumberScheduled"]
                    - cephfs_daemonset_status["numberReady"]
                )
                if (
                    rbd_unavailable
                    > upgrade_stats["odf_upgrade"]["rbd_max_unavailable"]
                ):
                    upgrade_stats["odf_upgrade"][
                        "rbd_max_unavailable"
                    ] = rbd_unavailable
                if (
                    cephfs_unavailable
                    > upgrade_stats["odf_upgrade"]["cephfs_max_unavailable"]
                ):
                    upgrade_stats["odf_upgrade"][
                        "cephfs_max_unavailable"
                    ] = cephfs_unavailable
                log.debug(f"rbd daemonset status: {rbd_daemonset_status}")
                log.debug(f"cephfs daemonset status: {cephfs_daemonset_status}")
            try:
                if sample:
                    log.info("Upgrade success!")
                    break
            except TimeoutException:
                raise TimeoutException("No new CSV found after upgrade!")
        if config.UPGRADE.get(
            "csi_rbd_plugin_update_strategy_max_unavailable_upgrade_middle"
        ) or config.UPGRADE.get(
            "csi_cephfs_plugin_update_strategy_max_unavailable_upgrade_middle"
        ):
            set_update_strategy(
                config.UPGRADE.get(
                    "csi_rbd_plugin_update_strategy_max_unavailable_upgrade_middle"
                ),
                config.UPGRADE.get(
                    "csi_cephfs_plugin_update_strategy_max_unavailable_upgrade_middle"
                ),
            )
        stop_time = time.time()
        time_taken = stop_time - start_time
        log.info(f"Upgrade took {time_taken} seconds to complete")
        if upgrade_stats:
            upgrade_stats["odf_upgrade"]["upgrade_time"] = time_taken
        old_image = upgrade_ocs.get_images_post_upgrade(
            channel, pre_upgrade_images, upgrade_version
        )

    # verify all required CSV's
    ocs_operator_names = get_required_csvs()
    channel = config.DEPLOYMENT.get("ocs_csv_channel")
    operator_selector = get_selector_for_ocs_operator()
    subscription_plan_approval = config.DEPLOYMENT.get("subscription_plan_approval")

    for ocs_operator_name in ocs_operator_names:
        package_manifest = PackageManifest(
            resource_name=ocs_operator_name,
            selector=operator_selector,
            subscription_plan_approval=subscription_plan_approval,
        )
        package_manifest.wait_for_resource(timeout=300)
        csv_name = package_manifest.get_current_csv(channel=channel)
        csv = CSV(resource_name=csv_name, namespace=namespace)
        csv.wait_for_phase("Succeeded", timeout=720)

    verify_image_versions(
        old_image,
        upgrade_ocs.get_parsed_versions()[1],
        upgrade_ocs.version_before_upgrade,
    )

    verify_nb_db_psql_version(check_image_name_version=False)

    upgrade_version_semantic = version.get_semantic_version(upgrade_version, True)
    # update external secrets
    if config.DEPLOYMENT["external_mode"]:
        if upgrade_version_semantic >= version.VERSION_4_10:
            external_cluster.update_permission_caps()
        else:
            external_cluster.update_permission_caps(EXTERNAL_CLUSTER_USER)
        external_cluster.get_external_cluster_details()

        # update the external cluster details in secrets
        log.info("updating external cluster secret")
        external_cluster_details = NamedTemporaryFile(
            mode="w+",
            prefix="external-cluster-details-",
            delete=False,
        )
        with open(external_cluster_details.name, "w") as fd:
            decoded_external_cluster_details = decode(
                config.EXTERNAL_MODE["external_cluster_details"]
            )
            fd.write(decoded_external_cluster_details)
        cmd = (
            f"oc set data secret/rook-ceph-external-cluster-details -n {config.ENV_DATA['cluster_namespace']} "
            f"--from-file=external_cluster_details={external_cluster_details.name}"
        )
        exec_cmd(cmd)

    if (
        platform in (constants.VSPHERE_PLATFORM, constants.IBMCLOUD_PLATFORM)
        and upgrade_version_semantic >= version.VERSION_4_18
    ):
        # using try/except to not fail deployments since these values are good to have
        # for vsphere platform
        try:
            set_ceph_config(
                entity="global",
                config_name="bluestore_slow_ops_warn_threshold",
                value="7",
            )
            set_ceph_config(
                entity="global",
                config_name="bluestore_slow_ops_warn_lifetime",
                value="10",
            )
        except Exception as ex:
            logger.error(
                f"Failed to set values for bluestore_slow_ops. Exception is: {ex}"
            )

    if config.ENV_DATA.get("mcg_only_deployment"):
        mcg_only_install_verification(ocs_registry_image=upgrade_ocs.ocs_registry_image)
    else:
        # below check is to make sure all the existing CSV's are in succeeded state and nothing
        # in pending state
        is_all_csvs_succeeded = check_all_csvs_are_succeeded(namespace=namespace)
        assert is_all_csvs_succeeded, "Not all CSV's are in succeeded state"
        if not config.DEPLOYMENT["external_mode"]:
            upgrade_version = version.get_semantic_version(upgrade_version, True)
        if (
            config.ENV_DATA.get("is_multus_enabled")
            and upgrade_version == version.VERSION_4_16
        ):
            from ocs_ci.helpers.helpers import upgrade_multus_holder_design

            upgrade_multus_holder_design()

        ocs_install_verification(
            timeout=600,
            skip_osd_distribution_check=True,
            ocs_registry_image=upgrade_ocs.ocs_registry_image,
            post_upgrade_verification=True,
            version_before_upgrade=upgrade_ocs.version_before_upgrade,
        )


def ocs_odf_upgrade_ui():
    """
    Function to upgrade OCS 4.8 to ODF 4.9 via UI on OCP 4.9
    Pass proper versions and upgrade_ui.yaml while running this function for validation to pass

    """
    set_update_strategy()
    login_ui()
    val_obj = ValidationUI()
    pagenav_obj = ValidationUI()
    dep_obj = DeploymentUI()
    dep_obj.operator = ODF_OPERATOR
    dep_obj.install_ocs_operator()
    original_ocs_version = config.ENV_DATA.get("ocs_version")
    upgrade_in_current_source = config.UPGRADE.get("upgrade_in_current_source", False)
    upgrade_ocs = OCSUpgrade(
        namespace=config.ENV_DATA["cluster_namespace"],
        version_before_upgrade=original_ocs_version,
        ocs_registry_image=config.UPGRADE.get("upgrade_ocs_registry_image"),
        upgrade_in_current_source=upgrade_in_current_source,
    )
    logger.info(
        "Click on Storage System under Provided APIs on Installed Operators Page"
    )
    val_obj.do_click(
        upgrade_ocs.validation_loc["storage-system-on-installed-operators"]
    )
    logger.info("Click on 'ocs-storagecluster-storagesystem' on Operator details page")
    val_obj.do_click(
        upgrade_ocs.validation_loc["ocs-storagecluster-storgesystem"],
        enable_screenshot=True,
    )
    logger.info("Click on Resources")
    val_obj.do_click(
        upgrade_ocs.validation_loc["resources-tab"], enable_screenshot=True
    )
    logger.info("Storage Cluster Status Check")
    storage_cluster_status_check = val_obj.wait_until_expected_text_is_found(
        locator=("//*[text()= 'Ready']", By.XPATH), expected_text="Ready", timeout=1200
    )
    assert (
        storage_cluster_status_check
    ), "Storage Cluster Status reported on UI is not 'Ready', Timeout 1200 seconds exceeded"
    logger.info(
        "Storage Cluster Status reported on UI is 'Ready', verification successful"
    )
    logger.info("Click on 'ocs-storagecluster")
    val_obj.do_click(upgrade_ocs.validation_loc["ocs-storagecluster"])
    val_obj.take_screenshot()
    pagenav_obj.odf_overview_ui()
    pagenav_obj.odf_storagesystems_ui()


def set_update_strategy(rbd_max_unavailable=None, cephfs_max_unavailable=None):
    """
    Update rook-ceph-operator-config configmap with parameters:
    CSI_RBD_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE and CSI_CEPHFS_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE.
    If values are not provided as parameters of this function then values are taken
    from ocs-ci config. If the values are not set in ocs-ci config or function
    parameters then they are not updated.

    Args:
        rbd_max_unavailable (int, str): Value of CSI_RBD_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE
            to be updated in rook-ceph-operator-config configmap.
        cephfs_max_unavailable (int, str): Value of CSI_CEPHFS_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE
            to be updated in rook-ceph-operator-config configmap.

    """
    rbd_max = rbd_max_unavailable or config.UPGRADE.get(
        "csi_rbd_plugin_update_strategy_max_unavailable"
    )
    cephfs_max = cephfs_max_unavailable or config.UPGRADE.get(
        "csi_cephfs_plugin_update_strategy_max_unavailable"
    )
    if rbd_max:
        config_map_patch = f'\'{{"data": {{"CSI_RBD_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE": "{rbd_max}"}}}}\''
        exec_cmd(
            f"oc patch configmap -n {config.ENV_DATA['cluster_namespace']} "
            f"{constants.ROOK_OPERATOR_CONFIGMAP} -p {config_map_patch}"
        )
        logger.info(
            f"CSI_RBD_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE is set to {rbd_max}"
        )
    if cephfs_max:
        config_map_patch = f'\'{{"data": {{"CSI_CEPHFS_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE": "{cephfs_max}"}}}}\''
        exec_cmd(
            f"oc patch configmap -n {config.ENV_DATA['cluster_namespace']} "
            f"{constants.ROOK_OPERATOR_CONFIGMAP} -p {config_map_patch}"
        )
        logger.info(
            f"CSI_CEPHFS_PLUGIN_UPDATE_STRATEGY_MAX_UNAVAILABLE is set to {rbd_max}"
        )
