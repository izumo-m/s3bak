# Appendix A. Setting up AWS

This appendix creates the two things [Getting started](02-getting-started.md)
asks you to have: a bucket to back up to, and an AWS profile on your machine
that can reach it. It is one-time work. Once it is done you never return here,
and nothing in the rest of the manual asks you to touch the AWS console again.

It assumes you already have an AWS account and can sign in to the console.
Creating an account, and the billing and root-user hardening that come with
it, are AWS's own subject and are out of scope here.

Steps 1 to 3 are done in the AWS Management Console. Its layout changes from
time to time, so they name what to create and which values matter rather than
which button to press. Steps 4 and 5 are done on your machine with the
[AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html);
install it before you start if you do not already have it.

s3bak itself never calls the AWS CLI. It is worth having anyway, because it
answers the two questions you will have if anything to do with credentials
ever goes wrong: which credentials am I actually using, and can they reach the
bucket at all. s3bak can only report what S3 told it; the AWS CLI can ask on
its own.

Throughout, substitute your own names for these:

- `my-bucket` — your bucket's name.
- `backup` — the path inside it that s3bak stores everything under.
- `s3bak` — the name of the IAM user, and of the profile on your machine.

## What you are creating

In this order:

1. **a bucket** — where the backup lives;
2. **a policy** — what may be done to it;
3. **a user** — who does it, carrying that policy;
4. **an access key** — how s3bak proves it is that user, stored on your
   machine as a named profile.

The user exists so that the credential sitting on your machine is not your own
account's. A backup runs unattended and keeps its key in a file; that key
should be able to do exactly one thing, which is write to one path of one
bucket. The policy in step 2 is what makes it so.

## 1. Create the bucket

In S3, create a bucket. These settings matter to you later:

- **Name.** It must be unique across all of AWS, not just your account, so
  personal names like `taro-backup-2026` are the norm. It becomes part of the
  `prefix` in your configuration file.
- **Region.** Pick one near you. Note which one you picked; you write it into
  the profile in step 4.

Everything else can stay at its default. Some of those defaults are worth
knowing you are keeping:

- **Block Public Access** stays on. Nothing in s3bak needs the bucket to be
  reachable without credentials.
- **Default encryption** is on, as SSE-S3. s3bak does not ask for any
  particular encryption mode and works with whatever the bucket applies.

Leave **versioning** off for now. It is what lets you go back to a previous
version of a file, or undo a deletion, and it is worth turning on later; but
it changes what the bucket costs and how it is cleaned up, so
[Operating s3bak](07-operating.md) covers it as a decision of its own.

You can store the backup at the bucket root, but a path inside the bucket is
better: it leaves room for the bucket to hold something else one day, and it
gives the policy in the next step something narrow to point at. This appendix
assumes the path `backup`, which pairs with a `prefix` of
`s3://my-bucket/backup` in your configuration file.

## 2. Create the policy

In IAM, create a customer managed policy from JSON. Paste this, replacing
`my-bucket` and `backup` with yours, and name it something like
`s3bak-backup`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListTheBackupPath",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::my-bucket",
      "Condition": {
        "StringLike": {
          "s3:prefix": "backup/*"
        }
      }
    },
    {
      "Sid": "ReadWriteBackupObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload"
      ],
      "Resource": "arn:aws:s3:::my-bucket/backup/*"
    }
  ]
}
```

The statements differ in what they point at, which is why they are separate.
Listing is something you do to a *bucket*, so the first names the bucket
itself and then restricts the answer to the backup path. Reading and writing
are things you do to *objects*, so the second names the objects under that
path.

Each permission pays for itself:

- **`s3:ListBucket`** — every comparison starts by listing what the backup
  holds.
- **`s3:GetObject`** — `pull`, `show`, `diff` and `verify` read objects, and
  every command reads the manifest.
- **`s3:PutObject`** — `push` writes objects and the manifest. A file large
  enough to be uploaded in parts is covered by this same permission.
- **`s3:DeleteObject`** — only `push --delete` removes anything, but without
  this that command fails partway rather than being refused up front.
- **`s3:AbortMultipartUpload`** — an interrupted upload of a large file cleans
  up the parts it already stored. Without this they stay in the bucket,
  invisible in a listing and charged for.

If you chose to store the backup at the bucket root instead, the object
resource becomes `arn:aws:s3:::my-bucket/*` and the `Condition` block goes
away.

What this policy does not grant is the point of it. A key carrying it cannot
read anything else in the bucket, cannot see your other buckets, cannot create
or delete a bucket, and cannot change the policy that restrains it.

## 3. Create the user

In IAM, create a user named `s3bak`. It needs **no console access**: this user
is never signed in as, only authenticated as by a program.

Attach the policy from step 2 to it directly. A group would be the better
answer if you were setting up several people; for one machine's backup, the
direct attachment is one less thing between you and what the user can do.

## 4. Create the access key and store it as a profile

Create an access key for that user, choosing the purpose that describes an
application running outside AWS. You get an access key ID and a secret access
key. **The secret is shown once**, so store it before leaving the page:

```sh
aws configure --profile s3bak
```

It asks for the key you were just shown, then for a default region, which
should be the one you created the bucket in, then for a default output format,
which is the AWS CLI's own affair and never reaches s3bak; `json` is fine.

The name after `--profile` is what the `profile` line in your s3bak
configuration file must match.

It writes two files, both with owner-only permissions:

- `~/.aws/credentials` gets a section `[s3bak]` holding the two keys;
- `~/.aws/config` gets a section `[profile s3bak]` holding the region and the
  output format. That header carries the word `profile`; the credentials
  file's header does not.

You can write the files by hand instead, in which case one section in
`~/.aws/config` can hold all of it:

```ini
[profile s3bak]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = ap-northeast-1
```

s3bak reads either layout. Written by hand, the file needs `chmod 600`: it
holds a secret in plain text and nothing will set the permissions for you.

An access key does not expire. If the machine holding it is lost or the key
ends up somewhere it should not be, delete the key in IAM: that is the whole
of the revocation, and it takes effect at once. Until then, what the key can
reach is what the policy in step 2 allows and nothing more, which is why it is
worth having written it narrowly.

## 5. Check that it works

Each of these commands answers a different question. Run them in this order,
so that a failure has as little as possible left to hide behind.

**Which credentials are in effect?**

```sh
aws configure list --profile s3bak
```

It prints the profile, the access key, the secret key and the region, each
with the source it came from. The keys are shown as their last four characters
only, which is enough to tell one key from another. If a value is missing, or
comes from somewhere you did not intend — an environment variable outranks the
files — this is where you find out.

**Does AWS accept them?**

```sh
aws sts get-caller-identity --profile s3bak
```

It answers with your account number and the ARN of the user you created in
step 3. The call needs no permission of its own, which is what makes it useful
here: it separates the two failures that look alike from a distance. A
credential AWS does not accept fails at this step; a credential that is
perfectly valid but not allowed near your bucket passes it and fails the next.

**Does the policy reach the bucket?**

```sh
aws s3 ls s3://my-bucket/backup/ --profile s3bak
```

It prints nothing, because nothing has been backed up yet, and nothing is the
answer you want: the request was allowed. Keep the path on the end. Listing
the bucket root, `s3://my-bucket/`, is refused by the policy from step 2, and
that refusal is the policy doing its job rather than a fault to correct.

Once they all pass, the AWS side is done. Go back to
[Getting started](02-getting-started.md) and write the configuration file.

## When something is refused

s3bak and the AWS CLI reach S3 through the same SDK, so both report these in
the same words, whether they turn up now or during a later backup:

- **`The config profile (...) could not be found`** — nothing under `~/.aws`
  defines that profile. Check the spelling, and that the header reads
  `[profile s3bak]` in `~/.aws/config` or `[s3bak]` in `~/.aws/credentials`.
- **`Unable to locate credentials`** — the section is there, but holds no
  `aws_access_key_id` / `aws_secret_access_key`.
- **`(InvalidAccessKeyId)`** — the access key ID is mistyped, or the key has
  been deleted in IAM.
- **`(SignatureDoesNotMatch)`** — the secret access key is wrong. A trailing
  space in the file counts as wrong.
- **`(NoSuchBucket)`** — the bucket name does not match any bucket, or the
  bucket belongs to another account.
- **`(AccessDenied)`** — the credentials are valid, but the policy does not
  cover what was asked for. The path after the bucket name must match the one
  in the policy's resource ARN, and s3bak's `prefix` must point inside it.
