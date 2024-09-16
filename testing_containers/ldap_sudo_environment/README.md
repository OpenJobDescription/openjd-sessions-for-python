
## Build
```
docker build --build-arg BUILDKIT_SANDBOX_HOSTNAME=ldap.environment.internal -t openjd_ldap_test testing_containers/ldap_sudo_environment
```

## Run Interactive Bash
To start an interactive bash session:
```
docker run -h ldap.environment.internal --rm -it  openjd_ldap_test:latest bash
```
To start the LDAP Server and Client:

```
/config/start_ldap.sh
```

Login via ldap:

```
login -p hostuser
```
