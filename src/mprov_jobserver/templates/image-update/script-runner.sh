#!/bin/bash

# grab some params
IMGDIR={{ imgDir }}
IMAGE={{ image }}

# Unmount the host's dev, proc, run and sys from the image however this
# script ends: left mounted, the image pack would archive the host's and an
# image delete would delete through them (#183).  A busy mount is detached
# lazily, so it leaves the image tree straight away.  The build checks for
# leftover mounts before packing.
cleanup() {
  for i in sys run proc dev
  do
    if mountpoint -q ${IMGDIR}/${i}; then
      umount ${IMGDIR}/${i} || umount -l ${IMGDIR}/${i}
    fi
  done
}
trap cleanup EXIT
# bash skips the EXIT trap when a signal kills it, so turn those into exits
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

for i in `echo "dev proc run sys"`
do
  mount -o bind /$i ${IMGDIR}/${i}
done 

# chroot into our IMGDIR and run the jobserver.
chroot $IMGDIR /bin/bash -c "/usr/local/bin/mprov_jobserver -c /tmp/mprov/jobserver.yaml -r -d -i $IMAGE"
# the image build fails if a script failed, so keep the job server's status (#221)
exit $?
