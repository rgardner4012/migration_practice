# VMware to Nutanix Migration Practice

A reference implementation for migrating VM workloads from VMware (managed via vRealize/vRA) to Nutanix with Terraform-managed infrastructure. The examples here demonstrate an approach to bulk migration at scale — manifest-driven, pipeline-friendly, scalable.

> **Note:** This is a practice repo built from provider documentation and migration tooling references. Without an active vCenter or Prism Central environment to test against, these examples are illustrative rather than production-ready. They show the shape of the approach.

## Background

The scenario this repo addresses: an organization running VMware with vRealize Automation and custom scripting wants to migrate to Nutanix while simultaneously replacing vRealize with Terraform and Ansible.

The source of truth for what these VMs *should be* lives in VMware today. Querying VMware first and generating a migration manifest turns the problem from "provision VMs on a new platform" into "translate VMware VM definitions into equivalent Nutanix VM definitions, then reconcile Terraform state with the migrated result."

## Migration approach

The end-to-end workflow this repo demonstrates:

1. Define the batch of systems/environments to migrate, accounting for bandwidth, storage, and environment dependencies.
2. Script discovery of supporting resources (port groups, datastores, resource pools, tags, etc) and create them as terraform modules following any architectural, policy, and naming standards.
3. Query vCenter for each VM's pertinent attributes and translate source constructs to target-specific resources (I.e. VLAN_PROD_WEBSERVERS -> prod-webtier via mapping table or translation rules).
4. Migrate VM's via scripted Nutanix Move using the target manifest from step 3.
5. Update the manifest with the Nutanix UUID and run terraform import.
6. Verify clean terraform plan, no destroy/create, no unexpected drift.
7. Apply any common Ansible roles, and ensure no changes, before adding the server to AAP.

## Manifest structure

The manifest is the heart of the approach. It's two things in one file:

- **A captured snapshot of source infrastructure** — what VMware says exists today.
- **A translation and decision record** — what each source construct maps to on Nutanix.

## Discovery

The discovery layer is two separate concerns.

**Infrastructure discovery** — finds every network, storage tier, and organizational construct the migrated VMs will depend on. The output is a list of Nutanix resources that need to exist before any VM migration begins. This drives the infrastructure-first Terraform workflow.

**VM discovery** — for each VM, captures the attributes needed to define an equivalent Nutanix VM. Uses pyVmomi for vSphere API access. The output is the `source` blocks of the migration manifest.

## Credential handling

The examples here use variables for credentials to keep this simple. In production, a different mechanism would be used (vault/IAM).

## What's not in this repo

Things that might be part of a real production deployment but are out of scope for a reference implementation:

- The actual GitLab CI pipeline definitions (these depend on the organization's runner topology and shared template structure)
- Integration with an existing CMDB or ServiceNow for approval workflows
- IPAM integration for target VM IP assignment
- Sentinel or OPA policies for Terraform plan evaluation
- Execution environment images for Ansible Automation Platform

Each of these would be layered on top of the patterns demonstrated here.
