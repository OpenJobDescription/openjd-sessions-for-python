# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

FROM python:3.9-buster

# Let our tests know that we"re in an environment that can run the sudo
# tests
ENV OPENJD_TEST_SUDO_TARGET_USER=targetuser
ENV OPENJD_TEST_SUDO_SHARED_GROUP=sharedgroup
ENV OPENJD_TEST_SUDO_DISJOINT_USER=disjointuser
ENV OPENJD_TEST_SUDO_DISJOINT_GROUP=disjointgroup

WORKDIR /config

COPY changePassword.ldif /config
COPY addUsersGroups.ldif /config
COPY addUsersToSharedGroup.ldif /config
COPY run_tests.sh  /config
COPY start_ldap.sh /config
COPY test_setup.sh /config

WORKDIR /code

# We set up two users for our tests:
#  1) hostuser -- the user that will be running the pytests.
#  2) targetuser -- the user that we'll be running subprocesses as in the
#     the cross-account tests.
#  3) disjointuser -- a user used in temporary directory creation tests.
# These accounts belong to the following groups:
#   hostuser: hostuser, sharedgroup
#   targetuser: targetuser, sharedgroup
#   disjointuser: disjointuser, disjointgroup
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive && \
    apt-get install -y vim screen slapd ldap-utils && \
    echo slapd   slapd/password1 password | debconf-set-selections -v && \
    echo slapd   slapd/password2 password | debconf-set-selections -v && \
    echo slapd   slapd/internal/adminpw  password | debconf-set-selections -v && \
    echo slapd   slapd/internal/generated_adminpw password | debconf-set-selections -v && \
    echo slapd   slapd/password_mismatch note | debconf-set-selections -v && \
    echo slapd   slapd/no_configuration  boolean false | debconf-set-selections -v && \
    echo slapd   slapd/dump_database     select  when needed | debconf-set-selections -v && \
    echo slapd   slapd/domain    string  environment.internal | debconf-set-selections -v && \
    echo slapd   slapd/move_old_database boolean true | debconf-set-selections -v && \
    echo slapd   slapd/postinst_error    note | debconf-set-selections -v && \
    echo slapd   slapd/purge_database    boolean false | debconf-set-selections -v && \
    echo slapd   slapd/dump_database_destdir     string  /var/backups/slapd-VERSION | debconf-set-selections -v && \
    echo slapd   shared/organization     string  environment.internal | debconf-set-selections -v && \
    echo slapd   slapd/invalid_config    boolean true | debconf-set-selections -v && \
    echo slapd   slapd/upgrade_slapcat_failure   error | debconf-set-selections -v && \
    dpkg-reconfigure slapd && \
    echo "BASE    dc=environment,dc=internal" >> /etc/ldap/ldap.conf && \
    echo "URI     ldap://ldap.environment.internal" >> /etc/ldap/ldap.conf && \
    update-rc.d slapd enable && service slapd start && \
    ldapmodify -Q -Y EXTERNAL -H ldapi:/// -f /config/changePassword.ldif && \
    ldapadd -x -D cn=admin,dc=environment,dc=internal -w password -f /config/addUsersGroups.ldif && \
    ldapmodify -xcD cn=admin,dc=environment,dc=internal -w password -f /config/addUsersToSharedGroup.ldif && \
    echo nslcd   nslcd/ldap-uris string  ldap://ldap.environment.internal/ | debconf-set-selections -v && \
    echo nslcd   nslcd/ldap-base string  dc=environment,dc=internal | debconf-set-selections -v && \
    echo libnss-ldapd:amd64      libnss-ldapd/nsswitch   multiselect     passwd, group, shadow | debconf-set-selections -v && \
    apt-get install -y libnss-ldapd libpam-ldapd && \
    echo session optional        pam_mkhomedir.so skel=/etc/skel umask=007 >> /etc/pam.d/common-session && \
    touch /etc/netgroup && service nscd restart && service nslcd restart && \
    # Create the home directories before login
    mkhomedir_helper hostuser 007 && mkhomedir_helper targetuser 007 && mkhomedir_helper disjointuser 007 && \
    chmod 777 /config/run_tests.sh && \
    chmod 777 /code && \
    # Set up the sudoers permissions
    apt-get install sudo-ldap && \
    echo "hostuser ALL=(${OPENJD_TEST_SUDO_TARGET_USER},hostuser) NOPASSWD: ALL" > /etc/sudoers.d/hostuser


WORKDIR /home/hostuser

CMD ["/bin/sh", "-c", "/config/start_ldap.sh && /config/test_setup.sh && sudo -u hostuser -i /home/hostuser/run_tests.sh"]