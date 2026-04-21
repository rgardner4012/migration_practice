# VMware to Nutanix Migration Practice

A reference implementation for migrating VM workloads from VMware (managed via vRealize/vRA) to Nutanix with Terraform-managed infrastructure. The examples here demonstrate an approach to bulk migration at scale — manifest-driven, pipeline-friendly, scalable.

> **Note:** This is a practice repo built from provider documentation and migration tooling references. Without an active vCenter or Prism Central environment to test against, these examples are illustrative rather than production-ready. They show the shape of the approach.

## Background

The scenario this repo addresses: an organization running VMware with vRealize Automation and custom scripting wants to migrate to Nutanix while simultaneously replacing vRealize with Terraform and Ansible.

The source of truth for what these VMs *should be* lives in VMware today. Querying VMware first and generating a migration manifest turns the problem from "provision VMs on a new platform" into "translate VMware VM definitions into equivalent Nutanix VM definitions, then reconcile Terraform state with the migrated result."

## Migration approach

The end-to-end workflow this repo demonstrates:

1. Group VMs into migration waves based on network bandwidth, storage size, application dependencies, and change windows.
2. Query vCenter for every portgroup, datastore, resource pool, and tag that migrated VMs will need equivalents for on Nutanix.
3. Create Nutanix subnets, storage containers, categories, and projects via Terraform before any VM migration begins.
4. For each VM, capture source attributes and map them to target Nutanix resources through translation rules.
5. Script the Move API to drive batch migration, capture new Nutanix UUIDs on completion.
6. Associate the migrated VM's new UUID with the pre-generated Terraform resource block.
7. Run `terraform plan` with `-detailed-exitcode` to confirm no drift between config and imported state.

