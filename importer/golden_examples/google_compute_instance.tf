# Golden example: Compute Engine instance (CC-9 P4-9b)
#
# CRITICAL hallucination-class fields to OMIT:
#   * NO bare `disks = [...]` -- the cloud's flat list shape doesn't
#     map to HCL. Use boot_disk + attached_disk blocks (the
#     COMPLEX_BLOCKS_TO_SKIP convention from detector/config.py).
#   * NO display_device.enable_display block -- cloud's nested form
#     vs HCL's flat enable_display field is a known asymmetry; OMIT.
#   * NO metadata.startup-script as a top-level field -- it nests
#     under metadata = { "startup-script" = "..." } map.
#
# Required: machine_type, network_interface block, boot_disk block.
# Recommended: shielded_instance_config (Secure Boot + vTPM +
#   Integrity Monitoring all true -- our gce_shielded_vm.rego rule
#   requires all three).

resource "google_compute_instance" "vm_example" {
  name         = "poc-vm-example"
  machine_type = "e2-medium"
  zone         = "us-central1-a"

  # DEDICATED service account, scoped to least privilege via
  # oauth_scopes. NOT the default Compute SA.
  service_account {
    email  = "poc-vm-sa@example-project.iam.gserviceaccount.com"
    scopes = ["cloud-platform"]
  }

  # All three Shielded VM knobs ON (our gce_shielded_vm.rego rule
  # requires all three -- stricter than Google's archived rule
  # which omitted vTPM).
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  boot_disk {
    initialize_params {
      image = "projects/debian-cloud/global/images/family/debian-12"
      size  = 20
      type  = "pd-balanced"
    }
    # CMEK on the boot disk -- our gce_disk_encryption.rego rule
    # requires this for regulated workloads.
    disk_encryption_key {
      kms_key_self_link = "projects/example-project/locations/us-central1/keyRings/poc-keyring/cryptoKeys/poc-vm-key"
    }
  }

  network_interface {
    network    = "projects/example-project/global/networks/default"
    subnetwork = "projects/example-project/regions/us-central1/subnetworks/default"
    # NO access_config block -- our gce_no_public_ip.rego rule requires
    # NOT having a public NAT unless the instance carries the
    # 'internet-facing' tag. This example is private.
  }

  labels = {
    team = "platform"
    env  = "prod"
  }

  metadata = {
    enable-oslogin = "TRUE"  # P2-14 quoted-enum: NOT bare boolean
  }

  lifecycle {
    ignore_changes = [
      metadata.startup-script,  # often computed
    ]
  }
}
