"""
Integration tests — require vcsim running via docker compose.

Start the simulator before running:
    docker compose -f docker-compose.vcsim.yml up -d
    VSPHERE_PASSWORD=pass pytest -m integration -v

The vcsim instance is configured with 2 DCs, 2 clusters per DC,
3 hosts per cluster, and 8 VMs per host (96 VMs total).
"""
import pytest
from pyVmomi import vim

from discoverVms import (
    aggregate_infrastructure,
    build_datastore_lookup,
    build_dvpg_key_lookup,
    build_portgroup_lookup,
    discover_vm,
    get_all_objects_of_type,
    vm_matches_filters,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def all_vms(vcenter_content):
    return get_all_objects_of_type(vcenter_content, vim.VirtualMachine)


@pytest.fixture(scope="module")
def non_template_vms(all_vms):
    return [vm for vm in all_vms if vm.config and not vm.config.template]


class TestSession:
    def test_service_instance_connected(self, vsphere_session):
        assert vsphere_session.service_instance is not None

    def test_content_accessible(self, vsphere_session):
        assert vsphere_session.content is not None
        assert vsphere_session.content.rootFolder is not None

    def test_rest_client_available(self, vsphere_session):
        assert vsphere_session.rest_client is not None


class TestVmEnumeration:
    def test_finds_vms(self, all_vms):
        assert len(all_vms) > 0

    def test_expected_vm_count(self, all_vms):
        # 2 DCs × 2 clusters × 3 hosts × 8 VMs = 96
        assert len(all_vms) >= 96

    def test_no_templates_in_non_template_list(self, non_template_vms):
        for vm in non_template_vms:
            assert not vm.config.template


class TestVmDiscovery:
    def test_discover_vm_has_required_fields(self, non_template_vms):
        vm = non_template_vms[0]
        discovered = discover_vm(vm, tag_resolver=None)

        assert discovered.name
        assert discovered.moid
        assert isinstance(discovered.cpu_count, int) and discovered.cpu_count > 0
        assert isinstance(discovered.memory_mb, int) and discovered.memory_mb > 0
        assert isinstance(discovered.disks, list)
        assert isinstance(discovered.networks, list)
        assert isinstance(discovered.datastores, list)
        assert isinstance(discovered.tags, dict)

    def test_discover_vm_power_state(self, non_template_vms):
        discovered = discover_vm(non_template_vms[0], tag_resolver=None)
        assert discovered.power_state in ("poweredOn", "poweredOff", "suspended", "unknown")

    def test_discover_vm_moid_matches_source(self, non_template_vms):
        vm = non_template_vms[0]
        discovered = discover_vm(vm, tag_resolver=None)
        assert discovered.moid == vm._moId

    def test_discover_multiple_vms(self, non_template_vms):
        sample = non_template_vms[:10]
        discovered = [discover_vm(vm, tag_resolver=None) for vm in sample]
        names = {d.name for d in discovered}
        assert len(names) == 10


class TestVmFilters:
    def test_no_filters_excludes_templates(self, all_vms):
        matched = [vm for vm in all_vms if vm_matches_filters(vm, None, None, None)]
        assert len(matched) > 0
        for vm in matched:
            assert vm.config is not None
            assert not vm.config.template

    def test_name_filter_exact_match(self, non_template_vms):
        target_name = non_template_vms[0].name
        matched = [vm for vm in non_template_vms if vm_matches_filters(vm, None, None, [target_name])]
        assert len(matched) == 1
        assert matched[0].name == target_name

    def test_name_filter_multiple(self, non_template_vms):
        names = [vm.name for vm in non_template_vms[:3]]
        matched = [vm for vm in non_template_vms if vm_matches_filters(vm, None, None, names)]
        assert len(matched) == 3

    def test_nonexistent_name_returns_empty(self, all_vms):
        matched = [vm for vm in all_vms if vm_matches_filters(vm, None, None, ["__no_such_vm__"])]
        assert matched == []

    def test_cluster_filter(self, non_template_vms):
        cluster_names = set()
        for vm in non_template_vms:
            host = getattr(vm.runtime, "host", None)
            if host:
                cluster = getattr(host, "parent", None)
                if isinstance(cluster, vim.ClusterComputeResource):
                    cluster_names.add(cluster.name)

        if not cluster_names:
            pytest.skip("No clustered VMs found in vcsim")

        target_cluster = next(iter(cluster_names))
        matched = [vm for vm in non_template_vms if vm_matches_filters(vm, None, target_cluster, None)]
        assert len(matched) > 0
        for vm in matched:
            host = getattr(vm.runtime, "host", None)
            cluster = getattr(host, "parent", None) if host else None
            assert cluster.name == target_cluster


class TestInfrastructureLookups:
    def test_portgroup_lookup_populated(self, vcenter_content):
        lookup = build_portgroup_lookup(vcenter_content)
        assert len(lookup) > 0

    def test_portgroup_lookup_name_consistency(self, vcenter_content):
        for name, info in build_portgroup_lookup(vcenter_content).items():
            assert info["name"] == name
            assert "type" in info
            assert "vlan_id" in info

    def test_datastore_lookup_populated(self, vcenter_content):
        lookup = build_datastore_lookup(vcenter_content)
        assert len(lookup) > 0

    def test_datastore_lookup_has_capacity(self, vcenter_content):
        for name, info in build_datastore_lookup(vcenter_content).items():
            assert info["name"] == name
            assert isinstance(info["capacity_gb"], float)
            assert info["capacity_gb"] > 0

    def test_dvpg_key_lookup_returns_dict(self, vcenter_content):
        # vcsim standard portgroups are not DVS-backed; empty dict is correct
        lookup = build_dvpg_key_lookup(vcenter_content)
        assert isinstance(lookup, dict)


class TestAggregateInfrastructure:
    def test_infrastructure_collected(self, non_template_vms, vcenter_content):
        discovered = [discover_vm(vm, tag_resolver=None) for vm in non_template_vms[:5]]
        infra = aggregate_infrastructure(discovered, vcenter_content)

        assert isinstance(infra.datastores, dict)
        assert isinstance(infra.portgroups, dict)
        assert isinstance(infra.clusters, set)
        assert isinstance(infra.resource_pools, set)

    def test_datastores_non_empty(self, non_template_vms, vcenter_content):
        discovered = [discover_vm(vm, tag_resolver=None) for vm in non_template_vms[:5]]
        infra = aggregate_infrastructure(discovered, vcenter_content)
        assert len(infra.datastores) > 0

    def test_clusters_non_empty(self, non_template_vms, vcenter_content):
        discovered = [discover_vm(vm, tag_resolver=None) for vm in non_template_vms[:10]]
        infra = aggregate_infrastructure(discovered, vcenter_content)
        assert len(infra.clusters) > 0
