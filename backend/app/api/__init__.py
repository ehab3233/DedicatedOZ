"""HTTP surface, split by audience.

* `auth`, `servers`, `jobs`, `ssh_keys`, `os_templates`, `console` — customers
* `admin` — staff only
* `boot` — machines being provisioned; see that module for its trust model
"""
