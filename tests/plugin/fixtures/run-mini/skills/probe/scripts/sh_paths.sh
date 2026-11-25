#!/usr/bin/env bash
rw_output_set paths "{\"kubeconfig\":\"${KUBECONFIG-unset}\",\"pgpassfile\":\"${PGPASSFILE-unset}\",\"sslCertFile\":\"${SSL_CERT_FILE-unset}\"}"
