import pytest

from discoverVms import (
    DiscoveredInfrastructure,
    DiscoveredVM,
    _safe_get,
    infrastructure_to_manifest,
    load_vm_names,
    vm_to_manifest_entry,
)

pytestmark = pytest.mark.unit


def _make_vm(**kwargs):
    defaults = dict(
        name="test-vm",
        uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        moid="vm-42",
        power_state="poweredOn",
        guest_os="Ubuntu Linux (64-bit)",
        cpu_count=4,
        cores_per_socket=2,
        memory_mb=8192,
        folder="Production/WebTier",
        resource_pool="rp-prod",
        cluster="cluster-01",
        host="esx-01.example.com",
        annotation="test annotation",
    )
    defaults.update(kwargs)
    return DiscoveredVM(**defaults)


class TestSafeGet:
    def test_returns_attribute(self):
        class Obj:
            x = 42

        assert _safe_get(Obj(), "x") == 42

    def test_returns_default_for_missing_attribute(self):
        assert _safe_get(object(), "missing", "fallback") == "fallback"

    def test_returns_default_when_value_is_none(self):
        class Obj:
            x = None

        assert _safe_get(Obj(), "x", "fallback") == "fallback"

    def test_returns_none_default_by_default(self):
        assert _safe_get(object(), "missing") is None


class TestLoadVmNames:
    def test_parses_names(self, tmp_path):
        f = tmp_path / "vms.txt"
        f.write_text("vm-1\nvm-2\nvm-3\n")
        assert load_vm_names(str(f)) == ["vm-1", "vm-2", "vm-3"]

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "vms.txt"
        f.write_text("vm-1\n\nvm-2\n\n")
        assert load_vm_names(str(f)) == ["vm-1", "vm-2"]

    def test_skips_comment_lines(self, tmp_path):
        f = tmp_path / "vms.txt"
        f.write_text("vm-1\n# this is a comment\nvm-2\n")
        assert load_vm_names(str(f)) == ["vm-1", "vm-2"]

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "vms.txt"
        f.write_text("  vm-with-spaces  \n")
        assert load_vm_names(str(f)) == ["vm-with-spaces"]


class TestVmToManifestEntry:
    def test_vm_name_at_top_level(self):
        entry = vm_to_manifest_entry(_make_vm())
        assert entry["vm_name"] == "test-vm"

    def test_source_block_fields(self):
        vm = _make_vm()
        src = vm_to_manifest_entry(vm)["source"]
        assert src["uuid"] == vm.uuid
        assert src["moid"] == vm.moid
        assert src["power_state"] == "poweredOn"
        assert src["guest_os"] == "Ubuntu Linux (64-bit)"
        assert src["cpu_count"] == 4
        assert src["cores_per_socket"] == 2
        assert src["memory_mb"] == 8192
        assert src["folder"] == "Production/WebTier"
        assert src["resource_pool"] == "rp-prod"
        assert src["cluster"] == "cluster-01"
        assert src["host"] == "esx-01.example.com"
        assert src["annotation"] == "test annotation"

    def test_target_block_is_empty_template(self):
        target = vm_to_manifest_entry(_make_vm())["target"]
        assert target["cluster"] is None
        assert target["subnet"] is None
        assert target["categories"] == {}
        assert target["disks"] == []

    def test_migration_block_defaults(self):
        migration = vm_to_manifest_entry(_make_vm())["migration"]
        assert migration["status"] == "pending"
        assert migration["wave"] is None
        assert migration["window"] is None
        assert migration["nutanix_vm_uuid"] is None

    def test_disks_networks_datastores_passed_through(self):
        disk = {
            "label": "Hard disk 1",
            "size_gb": 100.0,
            "thin_provisioned": True,
            "datastore": "ds1",
            "controller_key": 1000,
            "unit_number": 0,
        }
        nic = {
            "label": "Network adapter 1",
            "portgroup": "VM Network",
            "mac_address": "00:50:56:ab:cd:ef",
            "adapter_type": "vim.vm.device.VirtualVmxnet3",
        }
        vm = _make_vm(disks=[disk], networks=[nic], datastores=["ds1"])
        src = vm_to_manifest_entry(vm)["source"]
        assert src["disks"] == [disk]
        assert src["networks"] == [nic]
        assert src["datastores"] == ["ds1"]

    def test_tags_passed_through(self):
        vm = _make_vm(tags={"environment": ["prod"], "tier": ["web", "app"]})
        assert vm_to_manifest_entry(vm)["source"]["tags"] == {
            "environment": ["prod"],
            "tier": ["web", "app"],
        }


class TestInfrastructureToManifest:
    def _make_infra(self, **kwargs):
        defaults = dict(
            portgroups={
                "VM Network": {"name": "VM Network", "type": "Network", "vlan_id": None}
            },
            datastores={
                "datastore1": {"name": "datastore1", "type": "NFS", "capacity_gb": 500.0}
            },
            resource_pools={"rp-prod"},
            clusters={"cluster-01"},
            folders={"Production"},
            tag_categories={"environment": {"prod", "dev"}},
        )
        defaults.update(kwargs)
        return DiscoveredInfrastructure(**defaults)

    def test_metadata_fields(self):
        result = infrastructure_to_manifest(self._make_infra(), "vcenter.example.com")
        assert result["metadata"]["source_vcenter"] == "vcenter.example.com"
        assert "discovered_at" in result["metadata"]

    def test_portgroups_listed(self):
        result = infrastructure_to_manifest(self._make_infra(), "vc")
        assert len(result["portgroups"]) == 1
        assert result["portgroups"][0]["name"] == "VM Network"

    def test_datastores_listed(self):
        result = infrastructure_to_manifest(self._make_infra(), "vc")
        assert len(result["datastores"]) == 1
        assert result["datastores"][0]["name"] == "datastore1"

    def test_sets_sorted_to_lists(self):
        infra = self._make_infra(
            clusters={"zzz-cluster", "aaa-cluster"},
            resource_pools={"rp-b", "rp-a"},
            folders={"Z-folder", "A-folder"},
        )
        result = infrastructure_to_manifest(infra, "vc")
        assert result["clusters"] == ["aaa-cluster", "zzz-cluster"]
        assert result["resource_pools"] == ["rp-a", "rp-b"]
        assert result["folders"] == ["A-folder", "Z-folder"]

    def test_tag_categories_sorted(self):
        infra = self._make_infra(tag_categories={"env": {"prod", "dev", "staging"}})
        result = infrastructure_to_manifest(infra, "vc")
        assert result["tag_categories"]["env"] == ["dev", "prod", "staging"]

    def test_empty_infra(self):
        result = infrastructure_to_manifest(DiscoveredInfrastructure(), "vc")
        assert result["portgroups"] == []
        assert result["datastores"] == []
        assert result["clusters"] == []
        assert result["resource_pools"] == []
        assert result["folders"] == []
        assert result["tag_categories"] == {}
