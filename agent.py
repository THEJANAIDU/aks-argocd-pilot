"""Reviewed, explicit AKS deployment automation. No shell execution."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import yaml

MODE = "argocd"
class AgentError(Exception): pass

def run(args, data=None):
    try:
        result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=600, env={**os.environ,"GIT_TERMINAL_PROMPT":"0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError("Command unavailable or timed out: " + args[0]) from exc
    if result.returncode:
        # Tool stderr may contain credentials; never echo it into CI logs.
        raise AgentError("Command failed: " + args[0] + "; inspect locally with your operator credentials")
    return result.stdout

def load(path):
    c = json.loads(Path(path).read_text())
    required = {"subscription", "resource_group", "cluster", "context", "namespace", "application", "image", "repo_url", "revision"}
    required |= {"account", "org", "project", "pipeline", "input_sets", "delegate_image"} if MODE == "harness" else set()
    if set(c) != required: raise AgentError("Config fields must be exactly: " + ", ".join(sorted(required)))
    for k,v in c.items():
        if k == "input_sets":
            if not isinstance(v,list) or not v or any(not isinstance(x,str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",x) for x in v): raise AgentError("input_sets must contain identifiers")
        elif not isinstance(v,str) or not v or any(x in v.lower() for x in ("replace_me", "your_", "<", ">")):
            raise AgentError("Supply a real value for " + k)
    for k in ("namespace","application"):
        if len(c[k])>63 or not re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]|[a-z]",c[k]): raise AgentError("Invalid DNS label: " + k)
    if c["namespace"] in ("default","kube-system","argocd","harness-delegate"): raise AgentError("Use a dedicated application namespace")
    try: uuid.UUID(c["subscription"])
    except ValueError: raise AgentError("subscription must be a UUID")
    if c["image"].endswith("0"*64) or not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", c["image"]): raise AgentError("image must use an immutable sha256 digest")
    u=urllib.parse.urlparse(c["repo_url"])
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment: raise AgentError("Use a credential-free HTTPS repo_url")
    if c["revision"] == "0"*40 or not re.fullmatch(r"[a-f0-9]{40}",c["revision"]): raise AgentError("revision must be a full Git commit SHA")
    if MODE == "harness":
        for k in ("org","project","pipeline"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c[k]): raise AgentError("Invalid Harness identifier: " + k)
        if not re.fullmatch(r"[A-Za-z0-9_-]+",c["account"]): raise AgentError("Invalid account identifier")
        if not re.fullmatch(r"harness/delegate:[A-Za-z0-9._-]+@sha256:[a-f0-9]{64}",c["delegate_image"]): raise AgentError("Pin official harness/delegate tag and digest")
    return c

def kubectl(c,*args,data=None):
    return run(["kubectl","--context",c["context"],*args], data)

def doctor(c):
    for tool in ("az","kubectl","kubelogin"):
        if not shutil.which(tool): raise AgentError("Install prerequisite: " + tool)
    cluster=json.loads(run(["az","aks","show","--subscription",c["subscription"],"-g",c["resource_group"],"-n",c["cluster"],"-o","json"]))
    kc=json.loads(kubectl(c,"config","view","--minify","-o","json"))
    cluster_config=kc["clusters"][0]["cluster"]
    if cluster_config.get("insecure-skip-tls-verify"): raise AgentError("Kubeconfig must verify TLS certificates")
    server=urllib.parse.urlparse(cluster_config["server"]).hostname
    if server not in (cluster.get("fqdn"),cluster.get("privateFqdn")): raise AgentError("Kube context does not match the configured AKS cluster")
    kubectl(c,"get","--raw=/readyz")
    return "AKS identity, context and API reachability checked; authorization remains command-specific"

def application(c):
    name=c["application"]
    labels={"app":name}
    container={"name":name,"image":c["image"],"ports":[{"containerPort":8080}],"resources":{"requests":{"cpu":"50m","memory":"32Mi"},"limits":{"cpu":"250m","memory":"128Mi"}},"securityContext":{"allowPrivilegeEscalation":False,"readOnlyRootFilesystem":True,"capabilities":{"drop":["ALL"]}},"readinessProbe":{"httpGet":{"path":"/","port":8080},"initialDelaySeconds":3},"livenessProbe":{"httpGet":{"path":"/","port":8080},"initialDelaySeconds":10}}
    return [{"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":name,"namespace":c["namespace"]},"spec":{"replicas":2,"revisionHistoryLimit":5,"selector":{"matchLabels":labels},"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxUnavailable":0,"maxSurge":1}},"template":{"metadata":{"labels":labels},"spec":{"automountServiceAccountToken":False,"securityContext":{"runAsNonRoot":True,"runAsUser":10001,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[container]}}}},{"apiVersion":"v1","kind":"Service","metadata":{"name":name,"namespace":c["namespace"]},"spec":{"type":"ClusterIP","selector":labels,"ports":[{"port":80,"targetPort":8080}]}}]

def verify_source(c):
    """Check the actual pinned source before asking a deployment engine to reconcile it."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        run(["git","init","--quiet",tmp])
        run(["git","-C",tmp,"remote","add","origin",c["repo_url"]])
        run(["git","-C",tmp,"fetch","--quiet","--depth=1","origin",c["revision"]])
        actual=run(["git","-C",tmp,"rev-parse","FETCH_HEAD"]).strip()
        if actual != c["revision"]: raise AgentError("Fetched commit does not match requested revision")
        paths=run(["git","-C",tmp,"ls-tree","--name-only","FETCH_HEAD:deploy/app"]).splitlines()
        if paths != ["application.yaml"]: raise AgentError("deploy/app must contain only the generated application.yaml")
        raw=run(["git","-C",tmp,"show","FETCH_HEAD:deploy/app/application.yaml"])
        try: source=list(yaml.safe_load_all(raw))
        except yaml.YAMLError: raise AgentError("Source manifest YAML is invalid") from None
        if source != application(c): raise AgentError("Pinned source differs from the configured release; regenerate and commit deploy/app")

def namespace(c):
    return {"apiVersion":"v1","kind":"Namespace","metadata":{"name":c["namespace"],"labels":{"pod-security.kubernetes.io/enforce":"restricted"}}}

def write_yaml(path,docs):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(yaml.safe_dump_all(docs,sort_keys=False))

def render(c,out):
    out=Path(out)
    if out.exists(): raise AgentError("Output already exists; choose a new directory")
    out.mkdir(parents=True)
    write_yaml(out/"app"/"application.yaml",application(c))
    write_yaml(out/"platform"/"namespace.yaml",[namespace(c)])
    render_mode(c,out)
    return str(out.resolve())

COMMANDS=["validate","render","doctor","bootstrap","deploy","status"]
MUTATIONS={"bootstrap","deploy"}
VERSION="v3.5.6"
def objects(c):
    project={"apiVersion":"argoproj.io/v1alpha1","kind":"AppProject","metadata":{"name":c["application"],"namespace":"argocd"},"spec":{"sourceRepos":[c["repo_url"]],"destinations":[{"namespace":c["namespace"],"server":"https://kubernetes.default.svc"}],"clusterResourceWhitelist":[],"namespaceResourceWhitelist":[{"group":"apps","kind":"Deployment"},{"group":"","kind":"Service"}]}}
    app={"apiVersion":"argoproj.io/v1alpha1","kind":"Application","metadata":{"name":c["application"],"namespace":"argocd"},"spec":{"project":c["application"],"source":{"repoURL":c["repo_url"],"targetRevision":c["revision"],"path":"deploy/app"},"destination":{"server":"https://kubernetes.default.svc","namespace":c["namespace"]},"syncPolicy":{"automated":{"prune":False,"selfHeal":True},"syncOptions":["FailOnSharedResource=true"]}}}
    cluster={"apiVersion":"v1","kind":"Secret","metadata":{"name":"pilot-local-cluster","namespace":"argocd","labels":{"argocd.argoproj.io/secret-type":"cluster"}},"stringData":{"name":"in-cluster","server":"https://kubernetes.default.svc","namespaces":c["namespace"],"clusterResources":"false","config":"{}"}}
    return [cluster,project,app]

def render_mode(c,out):
    write_yaml(out/"argocd.yaml",objects(c))
    inclusions=[{"apiGroups":["apps"],"kinds":["Deployment","ReplicaSet"]},{"apiGroups":[""],"kinds":["Service","Pod","Event"]}]
    write_yaml(out/"platform"/"controller-config.yaml",[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"argocd-cm","namespace":"argocd"},"data":{"resource.inclusions":yaml.safe_dump(inclusions)}}])
    sa="argocd-application-controller"
    write_yaml(out/"platform"/"controller-rbac.yaml",[
      {"apiVersion":"rbac.authorization.k8s.io/v1","kind":"Role","metadata":{"name":"argocd-app","namespace":c["namespace"]},"rules":[{"apiGroups":["apps"],"resources":["deployments","replicasets"],"verbs":["get","list","watch","create","update","patch","delete"]},{"apiGroups":[""],"resources":["services"],"verbs":["get","list","watch","create","update","patch","delete"]},{"apiGroups":[""],"resources":["pods","events"],"verbs":["get","list","watch"]}]},
      {"apiVersion":"rbac.authorization.k8s.io/v1","kind":"RoleBinding","metadata":{"name":"argocd-app","namespace":c["namespace"]},"subjects":[{"kind":"ServiceAccount","name":sa,"namespace":"argocd"}],"roleRef":{"apiGroup":"rbac.authorization.k8s.io","kind":"Role","name":"argocd-app"}}])

def execute(c,a):
    doctor(c)
    if a.command=="bootstrap":
        vendor=Path(__file__).parent/"vendor"/"namespace-install.yaml"
        expected=(vendor.with_suffix(".sha256")).read_text().strip()
        data=vendor.read_bytes()
        if hashlib.sha256(data).hexdigest()!=expected: raise AgentError("Vendored installer checksum mismatch")
        kubectl(c,"apply","-f","-",data=yaml.safe_dump({"apiVersion":"v1","kind":"Namespace","metadata":{"name":"argocd"}}))
        for crd in sorted(vendor.parent.glob("*-crd.yaml")):
            raw=crd.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=crd.with_suffix(".sha256").read_text().strip(): raise AgentError("CRD checksum mismatch")
            kubectl(c,"apply","--server-side","-f","-",data=raw.decode())
        kubectl(c,"wait","--for=condition=Established","crd/applications.argoproj.io","crd/appprojects.argoproj.io","crd/applicationsets.argoproj.io","--timeout=120s")
        kubectl(c,"apply","--server-side","-n","argocd","-f","-",data=data.decode())
        kubectl(c,"rollout","status","deployment/argocd-server","-n","argocd","--timeout=300s")
        kubectl(c,"rollout","status","statefulset/argocd-application-controller","-n","argocd","--timeout=300s")
        print("Namespace-scoped Argo CD installed; review platform RBAC before deploy")
    elif a.command=="deploy":
        verify_source(c)
        # Regenerate from validated configuration; never execute an untrusted output directory.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/"rendered"; render(c,out)
            kubectl(c,"apply","-f",str(out/"platform"))
            kubectl(c,"apply","-f",str(out/"argocd.yaml"))
        print("Application submitted. Reconciliation is asynchronous; run status and inspect health.")
    else:
        result=json.loads(kubectl(c,"get","application",c["application"],"-n","argocd","-o","json"))
        status=result.get("status",{})
        print(json.dumps({"sync":status.get("sync",{}),"health":status.get("health",{}),"conditions":status.get("conditions",[])},indent=2))
        if status.get("sync",{}).get("status")!="Synced" or status.get("health",{}).get("status")!="Healthy": raise AgentError("Application is not yet Synced and Healthy")


def main(argv=None):
    p=argparse.ArgumentParser(description="AKS " + MODE + " automation agent")
    p.add_argument("command",choices=COMMANDS)
    p.add_argument("--config",required=True)
    p.add_argument("--output",default="generated")
    p.add_argument("--yes",action="store_true",help="Explicitly authorize the selected remote mutation")
    p.add_argument("--execution",help="Harness execution ID")
    a=p.parse_args(argv)
    try:
        c=load(a.config)
        if a.command=="validate": print("Configuration valid; credentials and remote resources not checked")
        elif a.command=="render": print(render(c,a.output))
        elif a.command=="doctor": print(doctor(c))
        else:
            if a.command in MUTATIONS and not a.yes: raise AgentError("Remote mutation requires --yes after reviewing rendered output")
            execute(c,a)
        return 0
    except (AgentError,ValueError,KeyError,OSError) as exc:
        print("ERROR: " + str(exc),file=sys.stderr)
        return 1

if __name__=="__main__": sys.exit(main())
