# Kubernetes Storage Lab · filesystem experiment

One Python service writes raw HTTP uploads to a mounted directory. The first experiment verifies that a file survives a pod replacement. Later, run the identical image with a Ceph RBD PVC to compare the two filesystem paths.

## 1. Run locally (Python 3.13)

From this directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

In a second terminal in the same directory:

```sh
printf 'Ali storage experiment 001\n' > sample.txt
python3 verify.py put sample.txt
python3 verify.py get sample.txt
```

PUT returns 204 after the file is published. GET returns 200 and the client checks the downloaded bytes against the original sample. Each command prints one JSON record with the operation, status, file size, elapsed seconds and original checksum. The timer covers the complete HTTP operation. Hashing and reading the local sample happen outside it. These single requests verify functionality. Comparative latency measurements require repeated runs with controlled client placement and concurrency.

## 2. Read the write path in app.py

Follow `put_file` → `publish`. Each PUT writes a private temporary file beside its destination. The service flushes and syncs the file, atomically renames it, then syncs the directory before acknowledging the request. A GET sees one complete version even during an overwrite. Concurrent successful PUTs to the same ID replace each other in rename order. A lost response can be retried with the same ID and bytes. Interrupted uploads normally clean up their temporary files; a hard process kill may leave a hidden staging file. A missing ID returns 404 and uploads over 64 MiB return 413. Use a dedicated writable directory on a Linux filesystem with rename and fsync support. This unauthenticated service is intended for an isolated lab.

## 3. Run the same image with Longhorn

Build for your worker architecture and publish the image to a registry the workers can pull from. Find their architecture with `kubectl get nodes -L kubernetes.io/arch`. For amd64 workers, from a Docker machine with registry access:

```sh
IMAGE=YOUR_REGISTRY/YOUR_PROJECT/storage-lab:1.1
docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
```

Replace the placeholder with your registry path. In `k8s/longhorn.yaml`, replace `image: storage-lab:1.1` with that same image reference. The manifest requests its own 1 GiB PVC and runs one application replica mounting it at /data. All kubectl commands below use the current namespace. Keep that namespace the same throughout.

```sh
kubectl apply -f k8s/longhorn.yaml
kubectl rollout status deployment/storage-lab
kubectl get pvc storage-lab-data
kubectl get pods -l app=storage-lab -o wide
kubectl port-forward deployment/storage-lab 8001:8000
```

Run the client on the machine where port-forward is running, from a second terminal. Keep the original sample there.

```sh
python3 verify.py put sample.txt --url http://127.0.0.1:8001/files/test-001
python3 verify.py get sample.txt --url http://127.0.0.1:8001/files/test-001
kubectl rollout restart deployment/storage-lab
kubectl rollout status deployment/storage-lab
kubectl get pods -l app=storage-lab -o wide
```

Port forwarding ends when the original pod exits. Restart the same port-forward command in the first terminal. Run **only GET** in the second terminal:

```sh
python3 verify.py get sample.txt --url http://127.0.0.1:8001/files/test-001
```

For the report, save the before/after pod names and nodes, bound PV name and both GET outputs. A matching checksum after pod replacement demonstrates persistence across that event. For comparative performance tests, place the client inside the cluster and keep its placement fixed; port-forward adds a separate network path.

## 4. Dashboard and recorded experiments

For the quickest local demo, leave the storage service running on port 8000. From a second terminal in this directory, with the virtual environment active, run:

```sh
python -m uvicorn dashboard:app --port 8080
```

Open http://127.0.0.1:8080. Select **Upload and verify**. The runner generates a unique ID and 1 MiB sample, records its expected SHA-256, then performs a PUT and GET. Select **Verify existing file** to perform only a GET against the original URL. Inspect shows request durations, status and errors; Export CSV includes all runs, raw requests, checksums and pod UIDs. Local runs report *Bytes verified* because Kubernetes identity is absent. The SQLite file defaults to ./results/runs.sqlite3. Keep the runner at one Uvicorn worker and one replica. An interrupted baseline remains in history. Each new run consumes 1 MiB on the target until its test data is removed.

For Kubernetes use the same namespace and image reference in both manifests. On the **runner node worker-1** prepare a new results directory with `sudo install -d -m 0750 -o 10001 -g 10001 /var/lib/storage-lab-results`. The dashboard manifest uses your existing lab-local StorageClass and a dedicated, retained local PV. Its node affinity fixes client placement. Choose a disk independent of backend disks when measuring resource contention. The results PV depends on the runner node remaining available.

```sh
kubectl apply -f k8s/longhorn.yaml
kubectl apply -f k8s/dashboard.yaml
kubectl rollout status deployment/storage-dashboard
kubectl port-forward deployment/storage-dashboard 8080:8080
```

Create a baseline in the browser. In another terminal run `kubectl rollout restart deployment/storage-lab` and `kubectl rollout status deployment/storage-lab`. Then select **Verify existing file**. The dashboard reports *Replacement verified* only when the GET matches the original checksum and the responding pod UID has changed since the successful baseline GET. Same-pod reads and unavailable pod identities have separate results. Rechecks append new request rows. Keep this dashboard private via local port forwarding. It has no user authentication. Configure the endpoint and backend label on the runner; the browser cannot supply a target URL.

For a subsequent Ceph RBD experiment, deploy a separate copy of the storage service with its own PVC, labels, and Service name. Point the runner at that Service with STORAGE_URL and change BACKEND_LABEL together; existing runs retain their original endpoint. Keep each Service exclusive to one backend and a single replica. The label is operator-supplied. For report evidence retain manifests, image digest, StorageClass, PV, replica settings and cluster versions alongside the CSV. This release records persistence experiments; cached single-request timings do not establish comparative throughput or p95 latency.
