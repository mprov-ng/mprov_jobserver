#!/bin/bash

IMGDIR={{ imgDir }}
IMAGE={{ image }}
for i in `echo "dev proc run sys"`
do
  mount -o bind /$i ${IMGDIR}/${i}
done 

export PYTHONPATH=/root/

chroot $IMGDIR /bin/bash -c "export PYTHONPATH=/root/; cd /root/mprov/mprov_jobserver; /usr/bin/python3 main.py -r -i $IMAGE"

for i in `echo "dev proc run sys"`
do
  umount ${IMGDIR}/${i}
done