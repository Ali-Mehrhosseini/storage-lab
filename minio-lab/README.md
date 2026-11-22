# Storage Lab · S3

A single-page S3 demo for MinIO and Amazon S3. Choose a sample size from
1 KiB to 16 MiB (default 1 MiB), then run a single target or compare both.
Save the run ID and backend to verify or download an existing object.
Existing MinIO objects remain accessible by their original run IDs.

## Configure the app

Create a test bucket on each backend you want to use. Give each app
identity permission to put and get objects under its bucket's
`experiments/` prefix. Set these variables on the server running the app.

| Variable | Value |
|---|---|
| `S3_ENDPOINT_URL` | MinIO S3 API address reachable from the app |
| `S3_BUCKET` | Existing MinIO bucket |
| `MINIO_ACCESS_KEY_ID`, `MINIO_SECRET_ACCESS_KEY` | MinIO credentials |
| `MINIO_REGION` | Optional MinIO signing region, defaults to `us-east-1` |
| `AWS_S3_BUCKET` | Existing Amazon S3 bucket |
| `AWS_S3_REGION` | Region of the Amazon S3 bucket, such as `eu-west-1` |
| `LAB_CLIENT_LOCATION` | Label for the app's location in the results, such as `AWS eu-west-1 / worker-2` |

Amazon S3 uses the standard Boto3 credential chain, including AWS workload
credentials, a local AWS profile, or the standard AWS credential environment
variables. For dual-backend mode, set the separate `MINIO_*` credentials.
MinIO-only installations can still use the old `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` variables for MinIO. In Kubernetes, supply credentials
through your workload identity or a Kubernetes Secret, with separate values
for each backend.

From the `minio-lab` directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --port 8080
```

Open http://127.0.0.1:8080. Use the page locally or through a private
port-forward. It has write access and no login.

To build a container, run `docker build -t storage-lab-minio .` from this
directory. The container listens on port 8080.

## Run the comparison

Run the app from one AWS pod with access to MinIO and to a same-region
Amazon S3 bucket. Click **Compare both · 3 trials**. Every trial sends
identical bytes to the two stores, reads both copies back, and checks the
original SHA-256. The app alternates which backend goes first. The page
shows each trial and the median PUT and GET times across pairs where both
checksums matched. If a pair fails, its failure is shown with the trial
details. These timings include the network path from the app to each store.
Record the client location and sample size alongside the results.

Each PUT creates a test object. Use a lifecycle rule on the
`experiments/` prefix or remove the test objects after collecting results.
