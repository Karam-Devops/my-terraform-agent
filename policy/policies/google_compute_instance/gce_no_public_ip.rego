# gce_no_public_ip.rego
#
# Compute instances MUST NOT have an external IP attached. The presence
# of any `accessConfigs` entry on any networkInterface means the VM has
# a public NAT — which means it's reachable from the open internet
# whether the security team knows about it or not.
#
# Standard exception path: tag the instance with `internet-facing` to
# acknowledge the risk. Untagged public IPs are the violation.
#
# Cloud fields:
#   networkInterfaces[].accessConfigs[]   (presence = public IP)
#   tags.items[]                          (look for "internet-facing")
#
# Severity: HIGH — directly exposes the VM to internet scanning.

package main

# An instance is internet-tagged if it has the explicit acknowledgment tag.
internet_tagged {
    input.tags.items[_] == "internet-facing"
}

# Set of interface indices that have at least one access config (= public IP).
public_interfaces[i] {
    input.networkInterfaces[i].accessConfigs[_]
}

deny[msg] {
    count(public_interfaces) > 0
    not internet_tagged
    msg := sprintf(
        "[HIGH][gce_no_public_ip] instance %s has external IP on %d network interface(s) without the 'internet-facing' tag",
        [input.name, count(public_interfaces)],
    )
}
