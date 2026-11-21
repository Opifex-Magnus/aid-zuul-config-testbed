"""Pinned provider routing: each provider's exact section, connection, label, limits and image (AIDepartment#1915).

No ZooKeeper or Kubernetes. Reads zuul.d/providers.yaml and, from the AID checkout that --launcher names, the
scheduler's zuul.conf (connection -> namespace) and each namespace's kustomization and job-node PodTemplate
(namespace -> node image). A change to any pinned value, here or in AID, fails until the pin is changed with it.
"""
import configparser
import unittest

import yaml

# Kubernetes providers: provider -> what it must route to. `node_image` is the PodTemplate's container image before
# the image workflow pins its digest: `zuul-node` is the PR node image, the check node's.
KUBERNETES = {
    'aid-k3s-check-pods': dict(
        section='aid-k3s-check-pods', connection='aid-check-pods', label='aid-check-pod-node',
        image='aid-check-pod-node', pods=3, namespace='zuul-nodes', template='node', node_image='zuul-node'),
    'aid-k3s-gate-pods': dict(
        section='aid-k3s-gate-pods', connection='aid-gate-pods', label='aid-gate-pod-node',
        image='aid-gate-pod-node', pods=1, namespace='zuul-gate', template='gate-node', node_image='zuul-gate-node'),
    'aid-k3s-post-pods': dict(
        section='aid-k3s-post-pods', connection='aid-post-pods', label='aid-post-pod-node',
        image='aid-post-pod-node', pods=1, namespace='zuul-post', template='post-node', node_image='zuul-post-node'),
    'aid-k3s-gate-compute-pods': dict(
        section='aid-k3s-gate-compute-pods', connection='aid-gate-compute-pods', label='aid-gate-compute-pod-node',
        image='aid-gate-compute-pod-node', pods=1, namespace='zuul-gate-compute', template='gate-compute-node',
        node_image='zuul-node'),
}
STAGING = dict(
    name='aid-thunderbox-staging', section='aid-thunderbox-staging', connection='aid-nodes', label='aid-staging-node',
    node='staging-node.zuul-staging.svc.cluster.local', port=22,
    host_key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILNgRXMvdQspHmm7GrBmWLPROwmBlrw1u5SbIUCUwFS1')


def kubernetes_facts(pin):
    """The facts a Kubernetes provider stanza and its section must state, from its pin."""
    return dict(
        section=pin['section'], connection=pin['connection'], launch_timeout=300, launch_attempts=1,
        flavors=[pin['image']], resource_limits={'pods': pin['pods']},
        labels=[dict(name=pin['label'], kind='pod', reuse=False, slots=1, spec={})],
        images=[dict(name=pin['image'], connection_type='ssh', connection_port=2222,
                     python_path='/usr/bin/python3', username='zuul')])


def check_pins(entries, aid_root):
    by_kind = {kind: {e[kind]['name']: e[kind] for e in entries if kind in e}
               for kind in ['section', 'provider', 'image', 'label', 'flavor']}
    sections, providers = by_kind['section'], by_kind['provider']
    zuul_conf = configparser.ConfigParser(interpolation=None)
    zuul_conf.read_string((aid_root / 'deploy/zuul/server/config/zuul.conf').read_text())

    def load(path):
        return yaml.safe_load((aid_root / path).read_text())

    class ProviderPins(unittest.TestCase):
        def test_every_provider_is_pinned(self):
            self.assertEqual(set(providers), set(KUBERNETES) | {STAGING['name']})

        def test_kubernetes_providers_route_to_their_pinned_section_connection_label_and_image(self):
            for name, pin in KUBERNETES.items():
                with self.subTest(provider=name):
                    stanza = providers[name]
                    section = sections[stanza['section']]
                    actual = dict(
                        section=stanza['section'], connection=section['connection'],
                        launch_timeout=section['launch-timeout'], launch_attempts=section['launch-attempts'],
                        flavors=[f['name'] for f in section['flavors']], resource_limits=stanza['resource-limits'],
                        labels=[{k.replace('-', '_'): v for k, v in label.items()} for label in stanza['labels']],
                        images=[{k.replace('-', '_'): v for k, v in image.items()} for image in stanza['images']])
                    self.assertEqual(actual, kubernetes_facts(pin))
                    self.assertEqual(by_kind['label'][pin['label']],
                                     dict(name=pin['label'], image=pin['image'], flavor=pin['image']))
                    self.assertEqual(by_kind['image'][pin['image']], dict(name=pin['image'], type='cloud'))
                    self.assertEqual(by_kind['flavor'][pin['image']], dict(name=pin['image']))

        def test_staging_stays_the_static_node(self):
            stanza = providers[STAGING['name']]
            section = sections[stanza['section']]
            self.assertEqual((stanza['section'], section['connection']), (STAGING['section'], STAGING['connection']))
            self.assertEqual(stanza['nodes'], [{'name': STAGING['node'], 'label': STAGING['label'],
                                                'connection-port': STAGING['port'], 'host-key': STAGING['host_key']}])
            self.assertEqual([l['name'] for l in stanza['labels']], [STAGING['label']])
            self.assertNotIn('resource-limits', stanza)

        def test_each_connection_selects_its_pinned_namespace(self):
            for name, pin in KUBERNETES.items():
                with self.subTest(provider=name):
                    connection = zuul_conf[f'connection "{pin["connection"]}"']
                    self.assertEqual((connection['driver'], connection['namespace']), ('kubernetes', pin['namespace']))
            namespaces = [pin['namespace'] for pin in KUBERNETES.values()]
            self.assertEqual(len(set(namespaces)), len(namespaces), 'two providers share a namespace')

        def test_each_namespace_runs_its_pinned_node_image(self):
            for name, pin in KUBERNETES.items():
                with self.subTest(provider=name):
                    directory = f'deploy/zuul/{pin["template"]}'
                    self.assertEqual(load(f'{directory}/kustomization.yaml')['namespace'], pin['namespace'])
                    template = load(f'{directory}/job-node.yaml')
                    self.assertEqual((template['kind'], template['metadata']['name']), ('PodTemplate', 'job-node'))
                    self.assertEqual([c['image'] for c in template['template']['spec']['containers']],
                                     [pin['node_image']])

        def test_gate_compute_runs_the_pr_image_not_the_gate_image(self):
            compute, check, gate = (KUBERNETES[n]['node_image'] for n in
                                    ('aid-k3s-gate-compute-pods', 'aid-k3s-check-pods', 'aid-k3s-gate-pods'))
            self.assertEqual(compute, check)
            self.assertNotEqual(compute, gate)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProviderPins))
    if not result.wasSuccessful():
        raise SystemExit(1)
