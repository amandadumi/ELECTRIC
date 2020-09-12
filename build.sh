cd modules/Tinker_ELECTRIC/dev
./full_build.sh
cd ../build/tinker/source
readlink -f dynamic.x > ../../../../test/locations/Tinker_electric
cd ../../../../ELECTRIC
cmake .
make
readlink -f ELECTRIC.py > ../../../../test/locations/ELECTRIC
