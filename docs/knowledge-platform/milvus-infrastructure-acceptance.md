# Milvus infrastructure boundary

The Platform release owns its Compose asset and infrastructure directories. Milvus
uses the same operator-supplied MinIO credentials as its object store. The variables
are documented by [Milvus 2.5](https://milvus.io/docs/v2.5.x/configure_minio.md).
There are no built-in credentials. The published host ports bind to 127.0.0.1.

Compose assigns container names from its project. For a separate installation,
set `PUDDINGKNOWLEDGE_PROJECT_NAME=puddingknowledge-<suffix>` when using
`assets/platform-infra.sh`, together with a distinct Platform Home and ports.
Keep that name for subsequent status/down/up operations. Before up/down, the
supervisor checks that every existing project container carries this exact Home
ownership label; missing or different ownership refuses the operation. Direct Compose users
must pass the matching `--project-name`. Separate names alone do not isolate
shared bind-mounted data: never share Home between independent instances.

etcd and MinIO must become healthy before Milvus starts; the API waits for Milvus
health. This is infrastructure readiness, not proof that any Collection index is
current, authorized, or activated. The release still requires explicitly supplied
API/worker/Console images; this change does not provide those container images.

## Reproducible local acceptance

With Docker running and the template's three Milvus infrastructure images cached,
run from the independent repository:

```sh
python3 scripts/acceptance/milvus_compose.py
```

The script starts only Milvus, etcd and MinIO, with random nondefault credentials,
a fresh temporary Home, a random project and two available loopback ports. It
creates a collection, inserts vectors, verifies search results, removes and
recreates the containers, and checks the persisted results again. It also verifies a wrong Home cannot stop the test project, and checks
that the previously running containers remain running. On completion it removes
only this test project's containers/network and prints a `proof.json` path.
The temporary data directory remains available for investigation; credentials
are not written to the proof. Docker necessarily receives them in container
environment variables while the test is running.

The test never pulls images, starts product API/worker/Console, or activates a
production index. An occupied port or unavailable dependency fails the run.
A hard interruption of the Python process may leave its explicitly printed test
project behind; inspect that exact project before manually cleaning it up.

Existing installations with fixed container names need an explicit maintenance
migration before adopting this asset: stop the old owned project, preserve its
Home, and recreate under the intended project name. This acceptance does not
prove upgrade of an existing stateful installation, nor full Milvus query/index
integration, reranking, or production readiness.
