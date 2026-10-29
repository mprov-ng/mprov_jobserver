#!/bin/bash

# grab some params
IMGDIR={{ imgDir }}
IMAGE={{ image }}

for i in `echo "dev proc run sys"`
do
  mount -o bind /$i ${IMGDIR}/${i}
done 

# chroot into our IMGDIR and run the jobserver.
chroot $IMGDIR /bin/bash -c "/usr/local/bin/mprov_jobserver -c /tmp/mprov/jobserver.yaml -r -d -i $IMAGE"
# the image build fails if a script failed, so keep the job server's status (#221)
rc=$?

for i in `echo "dev proc run sys"`
do
  umount ${IMGDIR}/${i}
done
exit $rc
