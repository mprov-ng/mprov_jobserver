FROM rockylinux:8
ARG MPROV_INSTALL_OPTS ""
COPY install_mprov_jobserver.sh /tmp
RUN chmod 755 /tmp/install_mprov_jobserver.sh
RUN /tmp/install_mprov_jobserver.sh -d ${MPROV_INSTALL_OPTS}
ENTRYPOINT ["/usr/local/bin/mprov_jobserver", "${MPROV_RUN_OPTS}"]
EXPOSE 80