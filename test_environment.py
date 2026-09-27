"""Read-only container-detection fixtures; no GPU, network, or process launch."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
PACKAGE = "doge_environment_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + ".nodes", ROOT / "nodes.py")
nodes = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = nodes
spec.loader.exec_module(nodes)

POD_UID = "11111111-2222-3333-4444-555555555555"
CONTAINER_ID = "a" * 64
HOST_MOUNTS = (
    "24 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
    "25 24 0:5 / /proc rw,nosuid,nodev,noexec - proc proc rw\n"
)
KUBELET_POD_MOUNT = (
    f"201 180 8:1 /var/lib/kubelet/pods/{POD_UID}/etc-hosts /etc/hosts rw,relatime - ext4 /dev/sda1 rw\n"
)
PROJECTED_SERVICEACCOUNT_MOUNT = (
    "202 180 0:47 / /var/run/secrets/kubernetes.io/serviceaccount ro,nosuid,nodev,noexec - tmpfs tmpfs ro,size=1024k\n"
)


class ContainerEnvironmentTests(unittest.TestCase):
    def detected(self, *, cgroup="0::/\n", mounts=HOST_MOUNTS, kubernetes_host=None):
        return nodes._container_evidence(cgroup, mounts, kubernetes_host)

    def test_host_is_rejected(self):
        self.assertFalse(self.detected())
        self.assertFalse(self.detected(cgroup="0::/system.slice/containerd.service\n"))
        self.assertFalse(self.detected(cgroup="0::/system.slice/docker.service\n"))

    def test_kubernetes_environment_variable_alone_is_rejected(self):
        self.assertFalse(self.detected(kubernetes_host="10.96.0.1"))

    def test_cgroup_v2_namespace_with_kubelet_pod_mount_is_accepted(self):
        self.assertTrue(self.detected(mounts=HOST_MOUNTS + KUBELET_POD_MOUNT, kubernetes_host="10.96.0.1"))

    def test_standard_serviceaccount_mount_supports_hidden_host_paths(self):
        for mountpoint in ("/var/run/secrets/kubernetes.io/serviceaccount", "/run/secrets/kubernetes.io/serviceaccount"):
            with self.subTest(mountpoint=mountpoint):
                mounts = PROJECTED_SERVICEACCOUNT_MOUNT.replace("/var/run/secrets/kubernetes.io/serviceaccount", mountpoint)
                self.assertTrue(self.detected(mounts=mounts, kubernetes_host="10.96.0.1"))

    def test_kubelet_host_mounts_without_pod_environment_are_rejected(self):
        self.assertFalse(self.detected(mounts=HOST_MOUNTS + KUBELET_POD_MOUNT))
        self.assertFalse(self.detected(mounts=PROJECTED_SERVICEACCOUNT_MOUNT, kubernetes_host=""))

    def test_kubepods_mount_root_is_accepted(self):
        mounts = f"209 180 0:29 /kubepods.slice/kubepods-burstable.slice/pod{POD_UID} /sys/fs/cgroup ro - cgroup2 cgroup ro\n"
        self.assertTrue(self.detected(mounts=mounts, kubernetes_host="10.96.0.1"))

    def test_docker_and_kubernetes_cgroup_paths_remain_supported(self):
        fixtures = (
            f"12:devices:/docker/{CONTAINER_ID}\n",
            f"0::/system.slice/docker-{CONTAINER_ID}.scope\n",
            f"0::/kubepods.slice/kubepods-burstable.slice/cri-containerd-{CONTAINER_ID}.scope\n",
        )
        for cgroup in fixtures:
            with self.subTest(cgroup=cgroup):
                self.assertTrue(self.detected(cgroup=cgroup))

    def test_incidental_and_malformed_mount_text_is_rejected(self):
        fixtures = (
            "201 180 8:1 / /data rw - ext4 /dev/sda1 rw,comment=kubepods\n",
            "201 180 8:1 / /data/var/run/secrets/kubernetes.io/serviceaccount rw - ext4 /dev/sda1 rw\n",
            "/var/run/secrets/kubernetes.io/serviceaccount\n",
            "201 180 8:1 / /var/run/secrets/kubernetes.io/serviceaccount rw\n",
        )
        for mounts in fixtures:
            with self.subTest(mounts=mounts):
                self.assertFalse(self.detected(mounts=mounts, kubernetes_host="10.96.0.1"))

    def test_wrapper_reads_kernel_evidence_without_reading_token(self):
        fixtures = {"/proc/1/cgroup": "0::/\n", "/proc/self/mountinfo": PROJECTED_SERVICEACCOUNT_MOUNT}
        reads = []

        def read_text(path, **kwargs):
            reads.append(str(path))
            return fixtures[str(path)]  # Any token/other path raises KeyError.

        with mock.patch.object(nodes.Path, "exists", return_value=False), \
                mock.patch.object(nodes.Path, "read_text", autospec=True, side_effect=read_text), \
                mock.patch.dict(nodes.os.environ, {"KUBERNETES_SERVICE_HOST": "10.96.0.1"}, clear=True):
            self.assertTrue(nodes.container_environment())
        self.assertEqual(reads, ["/proc/1/cgroup", "/proc/self/mountinfo"])

    def test_unreadable_kernel_evidence_fails_closed(self):
        with mock.patch.object(nodes.Path, "exists", return_value=False), \
                mock.patch.object(nodes.Path, "read_text", side_effect=PermissionError), \
                mock.patch.dict(nodes.os.environ, {"KUBERNETES_SERVICE_HOST": "10.96.0.1"}, clear=True):
            self.assertFalse(nodes.container_environment())

    def test_real_marker_does_not_require_kubernetes_environment(self):
        with mock.patch.object(nodes.Path, "exists", return_value=True), \
                mock.patch.object(nodes.Path, "read_text") as read_text:
            self.assertTrue(nodes.container_environment())
        read_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
