#!/usr/bin/env python3
"""
Discover VMs and supporting infrastructure from vCenter using the
Unified VCF SDK (VCF 9.0+).

The Unified VCF SDK (installed via `pip install vcf-sdk`) consolidates
pyVmomi (SOAP/Web Services) and the vSphere Automation SDK (REST/vAPI)
into a single package with shared authentication. This means one login
gets us access to both API families:

  * pyVmomi for detailed VM hardware inventory (disks, NICs, controllers)
  * vSphere Automation API for tags, categories, and attached tag queries

Tags in particular require the REST API — they are not accessible via
the SOAP/pyVmomi path. By sharing a session between the two clients, we
get full-fidelity discovery (including tags) from a single credential
acquisition.

This script produces two output manifests:

  1. infrastructure_manifest.yml — shared resources the migrated VMs
     depend on (networks, datastores, resource pools, folders, tag
     categories). Drives the "build target infrastructure first" step.

  2. migration_manifest.yml — per-VM source attributes captured from
     vSphere. The `target` and `migration` blocks are left empty for
     downstream translation and orchestration to fill in.

This script performs READ-ONLY operations against vCenter. Safe to run
against production environments during business hours.

Example:
    python3 discoverVms.py \\
        --vcenter vcenter.example.internal \\
        --username discovery-svc@vsphere.local \\
        --folder "Production/WebTier" \\
        --infra-output manifests/infrastructure.yml \\
        --vm-output manifests/migration.yml

Credentials:
    Password is read from the VSPHERE_PASSWORD environment variable. Do
    not pass passwords on the command line — they leak into shell history
    and process listings. In a pipeline context, fetch from your secrets
    broker (Vault, CyberArk, etc.) and export the variable before
    invoking this script.
"""
from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys
import urllib3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from http.cookies import SimpleCookie

try:
    import requests
    import yaml
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim
    from vmware.vapi.vsphere.client import create_vsphere_client
except ImportError as e:
    print(f"Missing dependency: {e}. Install with: pip install -r requirements.txt",
          file=sys.stderr)
    sys.exit(1)


log = logging.getLogger("discover_vms")


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredVM:
    """A single VM's source attributes captured from vSphere."""
    name: str
    uuid: str
    moid: str
    power_state: str
    guest_os: str
    cpu_count: int
    cores_per_socket: int
    memory_mb: int
    folder: str | None
    resource_pool: str | None
    cluster: str | None
    host: str | None
    annotation: str | None
    disks: list[dict[str, Any]] = field(default_factory=list)
    networks: list[dict[str, Any]] = field(default_factory=list)
    datastores: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class DiscoveredInfrastructure:
    """Aggregated supporting infrastructure referenced by the discovered VMs."""
    portgroups: dict[str, dict[str, Any]] = field(default_factory=dict)
    datastores: dict[str, dict[str, Any]] = field(default_factory=dict)
    resource_pools: set[str] = field(default_factory=set)
    clusters: set[str] = field(default_factory=set)
    folders: set[str] = field(default_factory=set)
    tag_categories: dict[str, set[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Unified authentication
# ---------------------------------------------------------------------------

class VsphereSession:
    """
    Holds a shared vSphere session spanning pyVmomi (SOAP) and the
    vSphere Automation SDK (REST).

    With the Unified VCF SDK both clients share a single session via the
    `vmware-api-session-id`. We connect once with pyVmomi, extract the
    session ID from the SOAP stub's cookie, and pass it into
    `create_vsphere_client` so no second login occurs. Closing the
    session invalidates it for both clients.
    """

    def __init__(self, host: str, username: str, password: str,
                 port: int = 443, insecure: bool = False):
        self.host = host
        self.port = port
        self.insecure = insecure

        ssl_context = ssl.create_default_context()
        if insecure:
            log.warning("SSL verification disabled — only acceptable for lab environments")
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        log.info("Connecting to %s:%d as %s (unified SDK)", host, port, username)
        try:
            self.service_instance = SmartConnect(
                host=host, user=username, pwd=password,
                port=port, sslContext=ssl_context,
            )
        except vim.fault.InvalidLogin:
            log.error("Invalid credentials for %s", host)
            sys.exit(2)
        except Exception as e:
            log.error("Failed to connect to %s: %s", host, e)
            sys.exit(2)

        self.content = self.service_instance.RetrieveContent()

        from http.cookies import SimpleCookie

        raw_cookie = self.service_instance._stub.cookie
        parsed = SimpleCookie()
        parsed.load(raw_cookie)

        session_id = None
        for name in ("vmware_soap_session", "vmware-api-session-id"):
            if name in parsed:
                session_id = parsed[name].value
                break

        if not session_id:
            raise RuntimeError(
                f"Could not extract session ID from cookie: {raw_cookie!r}"
            )

        self.http_session = requests.Session()
        self.http_session.verify = not insecure

        self.rest_client = create_vsphere_client(
            server=host,
            session=self.http_session,
            session_id=session_id,
        )

        log.debug("Unified session established — SOAP and REST share one auth")

    def close(self) -> None:
        try:
            Disconnect(self.service_instance)
            log.debug("Disconnected from vCenter")
        except Exception as e:
            log.warning("Error during disconnect: %s", e)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_all_objects_of_type(content, vim_type) -> list:
    """Return every managed object of the given vim type."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim_type], recursive=True
    )
    try:
        return list(view.view)
    finally:
        view.Destroy()


# ---------------------------------------------------------------------------
# VM filtering
# ---------------------------------------------------------------------------

def folder_path(vm) -> str | None:
    """Build the full folder path from VM up to the datacenter."""
    parts = []
    parent = vm.parent
    while parent is not None and not isinstance(parent, vim.Datacenter):
        if isinstance(parent, vim.Folder) and parent.name != "vm":
            parts.append(parent.name)
        parent = getattr(parent, "parent", None)
    return "/".join(reversed(parts)) if parts else None


def vm_matches_filters(vm, folder_filter: str | None,
                       cluster_filter: str | None,
                       name_filter: list[str] | None) -> bool:
    """Return True if the VM matches all provided filters."""
    if vm.config is None or vm.config.template:
        return False

    if name_filter and vm.name not in name_filter:
        return False

    if folder_filter:
        vm_folder = folder_path(vm) or ""
        if not vm_folder.startswith(folder_filter):
            return False

    if cluster_filter:
        host = getattr(vm.runtime, "host", None)
        cluster = getattr(host, "parent", None) if host else None
        cluster_name = cluster.name if cluster else None
        if cluster_name != cluster_filter:
            return False

    return True


# ---------------------------------------------------------------------------
# VM attribute extraction (pyVmomi / SOAP)
# ---------------------------------------------------------------------------

def extract_disks(vm) -> list[dict[str, Any]]:
    try:
        devices = vm.config.hardware.device
    except AttributeError:
        return []

    disks = []
    for device in devices:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue

        backing = device.backing
        thin = getattr(backing, "thinProvisioned", None)
        datastore_name = backing.datastore.name if getattr(backing, "datastore", None) else None

        disks.append({
            "label": device.deviceInfo.label,
            "size_gb": round(device.capacityInKB / 1024 / 1024, 2),
            "thin_provisioned": thin,
            "datastore": datastore_name,
            "controller_key": device.controllerKey,
            "unit_number": device.unitNumber,
        })
    return disks


def extract_networks(vm) -> list[dict[str, Any]]:
    try:
        devices = vm.config.hardware.device
    except AttributeError:
        return []

    networks = []
    for device in devices:
        if not isinstance(device, vim.vm.device.VirtualEthernetCard):
            continue

        portgroup = None
        backing = device.backing

        if isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
            portgroup = backing.deviceName
        elif isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
            port = backing.port
            portgroup = port.portgroupKey if port else None

        networks.append({
            "label": device.deviceInfo.label,
            "portgroup": portgroup,
            "mac_address": device.macAddress,
            "adapter_type": type(device).__name__,
        })
    return networks


def extract_datastores(vm) -> list[str]:
    return [ds.name for ds in (vm.datastore or [])]


def extract_cluster_and_host(vm) -> tuple[str | None, str | None]:
    try:
        runtime = vm.runtime
    except AttributeError:
        return None, None

    host = getattr(runtime, "host", None)
    if host is None:
        return None, None

    try:
        cluster = host.parent
        cluster_name = cluster.name if isinstance(cluster, vim.ClusterComputeResource) else None
    except AttributeError:
        cluster_name = None

    try:
        host_name = host.name
    except AttributeError:
        host_name = None

    return cluster_name, host_name

# ---------------------------------------------------------------------------
# Tag extraction (vSphere Automation / REST)
# ---------------------------------------------------------------------------

class TagResolver:
    """
    Resolves tag and category names for VMs via the vSphere Automation
    API. The REST tagging service returns tag IDs; we cache id → (name,
    category_name) lookups to avoid redundant API calls across many VMs.
    """

    def __init__(self, rest_client):
        self.client = rest_client
        self._tag_cache: dict[str, tuple[str, str]] = {}

    def tags_for_vm(self, vm_moid: str) -> dict[str, str]:
        """Return {category_name: tag_name} for a VM's attached tags."""
        try:
            dynamic_id = {"id": vm_moid, "type": "VirtualMachine"}
            tag_ids = self.client.tagging.TagAssociation.list_attached_tags(dynamic_id)
        except Exception as e:
            log.warning("Could not list attached tags for %s: %s", vm_moid, e)
            return {}

        result: dict[str, str] = {}
        for tag_id in tag_ids:
            try:
                tag_name, category_name = self._resolve(tag_id)
                result[category_name] = tag_name
            except Exception as e:
                log.warning("Could not resolve tag %s: %s", tag_id, e)
        return result

    def _resolve(self, tag_id: str) -> tuple[str, str]:
        if tag_id in self._tag_cache:
            return self._tag_cache[tag_id]

        tag = self.client.tagging.Tag.get(tag_id)
        category = self.client.tagging.Category.get(tag.category_id)
        self._tag_cache[tag_id] = (tag.name, category.name)
        return tag.name, category.name


# ---------------------------------------------------------------------------
# Discovery orchestration
# ---------------------------------------------------------------------------

def _safe_get(obj, attr, default=None):
    """Get an attribute, returning default if pyVmomi didn't materialize it."""
    try:
        value = getattr(obj, attr, default)
        return value if value is not None else default
    except AttributeError:
        return default


def discover_vm(vm, tag_resolver: TagResolver | None = None) -> DiscoveredVM:
    """Extract a full DiscoveredVM record from a vim.VirtualMachine."""
    config = _safe_get(vm, "config")
    hardware = _safe_get(config, "hardware") if config else None

    cluster_name, host_name = extract_cluster_and_host(vm)
    tags = tag_resolver.tags_for_vm(vm._moId) if tag_resolver else {}

    runtime = _safe_get(vm, "runtime")
    power_state = str(_safe_get(runtime, "powerState", "unknown")) if runtime else "unknown"

    resource_pool = _safe_get(vm, "resourcePool")
    rp_name = _safe_get(resource_pool, "name") if resource_pool else None

    return DiscoveredVM(
        name=vm.name,
        uuid=_safe_get(config, "uuid", "") if config else "",
        moid=vm._moId,
        power_state=power_state,
        guest_os=_safe_get(config, "guestFullName", "unknown") if config else "unknown",
        cpu_count=_safe_get(hardware, "numCPU", 0) if hardware else 0,
        cores_per_socket=_safe_get(hardware, "numCoresPerSocket", 0) if hardware else 0,
        memory_mb=_safe_get(hardware, "memoryMB", 0) if hardware else 0,
        folder=folder_path(vm),
        resource_pool=rp_name,
        cluster=cluster_name,
        host=host_name,
        annotation=_safe_get(config, "annotation") if config else None,
        disks=extract_disks(vm) if config and hardware else [],
        networks=extract_networks(vm) if config and hardware else [],
        datastores=extract_datastores(vm),
        tags=tags,
    )

def aggregate_infrastructure(vms: list[DiscoveredVM],
                             content) -> DiscoveredInfrastructure:
    """Walk the discovered VMs and build the shared-infrastructure manifest."""
    infra = DiscoveredInfrastructure()

    portgroup_lookup = build_portgroup_lookup(content)
    datastore_lookup = build_datastore_lookup(content)

    for vm in vms:
        if vm.folder:
            infra.folders.add(vm.folder)
        if vm.resource_pool:
            infra.resource_pools.add(vm.resource_pool)
        if vm.cluster:
            infra.clusters.add(vm.cluster)

        for nic in vm.networks:
            pg_name = nic["portgroup"]
            if pg_name and pg_name not in infra.portgroups:
                infra.portgroups[pg_name] = portgroup_lookup.get(pg_name, {
                    "name": pg_name,
                    "type": "unknown",
                    "vlan_id": None,
                })

        for ds_name in vm.datastores:
            if ds_name not in infra.datastores:
                infra.datastores[ds_name] = datastore_lookup.get(ds_name, {
                    "name": ds_name,
                    "type": "unknown",
                    "capacity_gb": None,
                })

        for category, value in vm.tags.items():
            infra.tag_categories.setdefault(category, set()).add(value)

    return infra


def build_portgroup_lookup(content) -> dict[str, dict[str, Any]]:
    """Index all portgroups (standard + distributed) by name."""
    lookup: dict[str, dict[str, Any]] = {}

    for net in get_all_objects_of_type(content, vim.Network):
        info: dict[str, Any] = {
            "name": net.name,
            "type": type(net).__name__,
            "vlan_id": None,
        }
        if isinstance(net, vim.dvs.DistributedVirtualPortgroup):
            cfg = getattr(net, "config", None)
            default_cfg = getattr(cfg, "defaultPortConfig", None) if cfg else None
            vlan = getattr(default_cfg, "vlan", None) if default_cfg else None
            if vlan is not None:
                info["vlan_id"] = getattr(vlan, "vlanId", None)
            dvs = getattr(cfg, "distributedVirtualSwitch", None) if cfg else None
            info["switch"] = dvs.name if dvs else None
        lookup[net.name] = info

    return lookup


def build_datastore_lookup(content) -> dict[str, dict[str, Any]]:
    """Index all datastores by name."""
    lookup: dict[str, dict[str, Any]] = {}
    for ds in get_all_objects_of_type(content, vim.Datastore):
        summary = ds.summary
        lookup[ds.name] = {
            "name": ds.name,
            "type": summary.type,
            "capacity_gb": round(summary.capacity / (1024 ** 3), 2),
            "free_gb": round(summary.freeSpace / (1024 ** 3), 2),
        }
    return lookup


# ---------------------------------------------------------------------------
# Manifest serialization
# ---------------------------------------------------------------------------

def vm_to_manifest_entry(vm: DiscoveredVM) -> dict[str, Any]:
    return {
        "vm_name": vm.name,
        "source": {
            "uuid": vm.uuid,
            "moid": vm.moid,
            "power_state": vm.power_state,
            "guest_os": vm.guest_os,
            "folder": vm.folder,
            "resource_pool": vm.resource_pool,
            "cluster": vm.cluster,
            "host": vm.host,
            "annotation": vm.annotation,
            "cpu_count": vm.cpu_count,
            "cores_per_socket": vm.cores_per_socket,
            "memory_mb": vm.memory_mb,
            "disks": vm.disks,
            "networks": vm.networks,
            "datastores": vm.datastores,
            "tags": vm.tags,
        },
        "target": {
            "cluster": None,
            "subnet": None,
            "categories": {},
            "disks": [],
        },
        "migration": {
            "wave": None,
            "window": None,
            "status": "pending",
            "nutanix_vm_uuid": None,
        },
    }


def infrastructure_to_manifest(infra: DiscoveredInfrastructure,
                               source_vcenter: str) -> dict[str, Any]:
    return {
        "metadata": {
            "source_vcenter": source_vcenter,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        },
        "portgroups": sorted(infra.portgroups.values(), key=lambda p: p["name"]),
        "datastores": sorted(infra.datastores.values(), key=lambda d: d["name"]),
        "clusters": sorted(infra.clusters),
        "resource_pools": sorted(infra.resource_pools),
        "folders": sorted(infra.folders),
        "tag_categories": {
            category: sorted(values)
            for category, values in sorted(infra.tag_categories.items())
        },
    }


def write_yaml(path: str, data: dict[str, Any]) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    log.info("Wrote %s", path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover VMs and supporting infrastructure from vCenter (VCF SDK 9.0+)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--vcenter", required=True, help="vCenter hostname")
    parser.add_argument("--port", type=int, default=443, help="vCenter port (default: 443)")
    parser.add_argument("--username", required=True,
                        help="vSphere username (password from VSPHERE_PASSWORD env)")
    parser.add_argument("--insecure", action="store_true",
                        help="Disable SSL verification (lab only)")
    parser.add_argument("--skip-tags", action="store_true",
                        help="Skip tag discovery (faster for large environments)")

    filter_group = parser.add_argument_group("VM filters")
    filter_group.add_argument("--folder", help="Only discover VMs in this folder path")
    filter_group.add_argument("--cluster", help="Only discover VMs in this cluster")
    filter_group.add_argument("--vm-list", help="File containing VM names, one per line")

    output_group = parser.add_argument_group("Output")
    output_group.add_argument("--infra-output", default="infrastructure_manifest.yml",
                              help="Path to write the infrastructure manifest")
    output_group.add_argument("--vm-output", default="migration_manifest.yml",
                              help="Path to write the VM migration manifest")
    output_group.add_argument("--verbose", "-v", action="store_true",
                              help="Enable debug logging")

    return parser.parse_args()


def load_vm_names(path: str) -> list[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    password = os.environ.get("VSPHERE_PASSWORD")
    if not password:
        log.error("VSPHERE_PASSWORD environment variable is not set")
        return 2

    name_filter = load_vm_names(args.vm_list) if args.vm_list else None

    with VsphereSession(args.vcenter, args.username, password,
                        port=args.port, insecure=args.insecure) as session:

        log.info("Enumerating VMs in %s", args.vcenter)
        all_vms = get_all_objects_of_type(session.content, vim.VirtualMachine)
        log.info("Found %d total VMs in vCenter", len(all_vms))

        matched = [
            vm for vm in all_vms
            if vm_matches_filters(vm, args.folder, args.cluster, name_filter)
        ]
        log.info("%d VMs match the provided filters", len(matched))

        if not matched:
            log.warning("No VMs matched — check your filters")
            return 1

        tag_resolver = None if args.skip_tags else TagResolver(session.rest_client)
        if args.skip_tags:
            log.info("Tag discovery disabled via --skip-tags")

        discovered = []
        for vm in matched:
            try:
                discovered.append(discover_vm(vm, tag_resolver))
                log.debug("Discovered %s", vm.name)
            except Exception as e:
                log.error("Failed to discover %s: %s: %s", vm.name, type(e).__name__, e, exc_info=args.verbose)

        log.info("Successfully discovered %d VMs", len(discovered))

        infra = aggregate_infrastructure(discovered, session.content)
        log.info(
            "Infrastructure: %d portgroups, %d datastores, %d clusters, "
            "%d resource pools, %d folders, %d tag categories",
            len(infra.portgroups), len(infra.datastores), len(infra.clusters),
            len(infra.resource_pools), len(infra.folders), len(infra.tag_categories),
        )

        write_yaml(args.infra_output,
                   infrastructure_to_manifest(infra, args.vcenter))
        write_yaml(args.vm_output,
                   {"vms": [vm_to_manifest_entry(v) for v in discovered]})

    return 0


if __name__ == "__main__":
    sys.exit(main())
