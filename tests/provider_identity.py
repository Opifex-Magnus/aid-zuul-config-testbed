"""Pinned launcher regression: retained static allocations across a driver switch.

No ZooKeeper, Kubernetes or publishing credentials. Exercise Zuul's actual retained
node selection, canonical provider lookup and static deletion state machine.
"""
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import Mock

import yaml
from zuul.driver.kubernetes.kubernetesmodel import KubernetesProviderNode
from zuul.driver.static.staticmodel import StaticProviderNode
from zuul.driver.static.staticprovider import StaticProvider
from zuul.launcher.server import LabelNodeMapping, Launcher
from zuul.provider import CNameMixin

PROJECT = 'github.com/kristofk/aid-zuul-config'
RETIRED = (
    ('aid-k3s', 'aid-node', 'aid-check-pods'),
    ('aid-k3s-gate', 'aid-gate-node', 'aid-gate-pods'),
    ('aid-k3s-post', 'aid-post-node', 'aid-post-pods'),
)


def canonical(name):
    return CNameMixin.canonical_name.fget(S(name=name, project_canonical_name=PROJECT))


def check_identity(entries, aid_root):
    sections = {e['section']['name']: e['section'] for e in entries if 'section' in e}
    providers = {sections[e['provider']['section']]['connection']: e['provider']
                 for e in entries if 'provider' in e}

    class RetainedStaticNodes(unittest.TestCase):
        def test_retained_static_nodes_cannot_satisfy_disposable_requests(self):
            for old_provider, old_label, connection in RETIRED:
                stanza = providers[connection]
                provider = S(canonical_name=canonical(stanza['name']))
                labels = [label['name'] for label in stanza['labels']]
                launcher = S(tenant_providers={'aid': [provider]},
                             api=S(hasNodeExpired=lambda node: False))
                request = S(tenant_name='aid', labels=labels, image_upload_uuid=None)
                with self.subTest(connection=connection):
                    self.assertNotEqual(provider.canonical_name, canonical(old_provider))
                    self.assertNotIn(old_label, labels)
                    self.assertTrue(all(not label.get('aliases') for label in stanza['labels']))
                    # First reproduce the real retained record. Then isolate each
                    # barrier: old provider with a new label; old label with the
                    # new provider. Both changes are necessary migration invariants.
                    for recorded_provider, recorded_label in (
                        (canonical(old_provider), old_label),
                        (canonical(old_provider), labels[0]),
                        (provider.canonical_name, old_label),
                    ):
                        node = StaticProviderNode()
                        node._set(provider=recorded_provider, label=recorded_label,
                                  state=node.State.READY, reuse=True, connection_name='aid-nodes',
                                  interface_ip='post-node-0.post-node.zuul-post.svc.cluster.local',
                                  connection_port=2222)
                        unassigned = LabelNodeMapping()
                        unassigned.addNode(node)
                        selected = Launcher._filterUnassignedNodes(launcher, unassigned, request)
                        self.assertEqual([n for label in labels for n in selected.getNodesForLabel(label)], [])
                    # Positive control: a fresh pod node remains assignable.
                    node = KubernetesProviderNode()
                    node._set(provider=provider.canonical_name, label=labels[0], state=node.State.READY)
                    unassigned = LabelNodeMapping()
                    unassigned.addNode(node)
                    selected = Launcher._filterUnassignedNodes(launcher, unassigned, request)
                    self.assertEqual(selected.getNodesForLabel(labels[0]), [node])

        def test_old_canonical_identity_never_resolves_to_kubernetes_cleanup(self):
            launcher = S(tenant_providers={'aid': [
                S(canonical_name=canonical(providers[connection]['name']))
                for _, _, connection in RETIRED]})
            launcher._getProviderByCanonicalName = lambda name: Launcher._getProviderByCanonicalName(launcher, name)
            for old_provider, old_label, connection in RETIRED:
                with self.subTest(connection=connection):
                    node = StaticProviderNode()
                    node._set(provider=canonical(old_provider), label=old_label, state=node.State.USED, reuse=True)
                    with self.assertRaisesRegex(Exception, 'Unable to find'):
                        Launcher._getProviderForNode(launcher, node, ignore_label=True)
                    # Retire a drained, deregistered static node with its own
                    # driver, which must not read kubernetes_namespace_id or
                    # call any Kubernetes endpoint. The live ownership/locking
                    # prerequisite is separate from this no-I/O state machine.
                    static = StaticProvider()
                    static._set(nodes={})
                    machine = static.getDeleteStateMachine(node, Mock())
                    machine.advance()
                    self.assertTrue(machine.complete)
                    self.assertEqual(node.state, node.State.USED)
                    fresh = KubernetesProviderNode()
                    fresh._set(provider=canonical(providers[connection]['name']))
                    self.assertEqual(Launcher._getProviderForNode(launcher, fresh).canonical_name, fresh.provider)

        def test_nodesets_and_compose_fixture_use_the_new_labels(self):
            root = Path(__file__).resolve().parents[1]
            def load(path):
                return yaml.safe_load(path.read_text())
            def base_nodes(entries):
                return next(e['job']['nodeset']['nodes'] for e in entries
                            if 'job' in e and e['job']['name'] == 'base')
            def nodeset(entries, name):
                return next(e['nodeset']['nodes'] for e in entries
                            if 'nodeset' in e and e['nodeset']['name'] == name)
            check = providers['aid-check-pods']['labels'][0]['name']
            gate = providers['aid-gate-pods']['labels'][0]['name']
            post = providers['aid-post-pods']['labels'][0]['name']
            self.assertEqual([n['label'] for n in base_nodes(load(root / 'zuul.d/jobs.yaml'))], [check])
            self.assertEqual([n['label'] for n in nodeset(load(root / 'zuul.d/publish.yaml'), 'aid-post')], [post])
            aid_jobs = load(aid_root / 'ci/zuul/zuul.d/jobs.yaml')
            self.assertEqual([n['label'] for n in nodeset(aid_jobs, 'aid-gate-image')], [gate])
            self.assertEqual([n['label'] for n in nodeset(aid_jobs, 'aid-staging')], ['aid-staging-node'])
            fixture = load(aid_root / 'ci/zuul/testbed/providers.yaml')
            fixture_labels = {e['label']['name'] for e in fixture if 'label' in e}
            self.assertTrue({check, gate, 'aid-staging-node'} <= fixture_labels)
            static = providers['aid-nodes']
            self.assertEqual(static['name'], 'aid-thunderbox-staging')
            self.assertEqual([n['label'] for n in static['nodes']], ['aid-staging-node'])

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RetainedStaticNodes))
    if not result.wasSuccessful():
        raise SystemExit(1)
