import re
from datetime import timedelta
from enum import Enum
from urlparse import urlparse
from app.pycgminer.pycgminer import CgminerAPI
from app.views.antminer_json import (get_summary,
                                     get_pools,
                                     get_stats,
                                     )
from app.models import MinerModel

def dedupe_messages(l):
    s = set()
    for msg in l:
        s.add(msg)
    return list(s)


def detect_model(ip):
    stats = get_stats(ip)

    # Check for connectivity error.
    if stats['STATUS'][0]['STATUS'] == 'error':
        raise Exception("[ERROR] Error while connecting to miner at ip address '{}'.".format(
            ip))

    # Try identifying the device.
    model_name = None
    if 'Type' in stats['STATS'][0]:
        models = re.findall(r'Antminer (\w*\+?)', stats['STATS'][0]['Type'])
        if len(models) == 1:
            model_name = models[0]
    elif 'ID' in stats['STATS'][0]:
        # ID are used for devices like Avalon.
        model_name = stats['STATS'][0]['ID']
        if model_name == "AV70":
            model_name = "AV741"
        elif model_name == "GSD0":
            model_name = "GekkoScience"
        elif model_name == "ANTR10":
            model_name = "R1-LTC"

    if not model_name is None:
        model = MinerModel.query.filter_by(model=model_name).first()
        if not model is None:
            return model
    else:
        model_name = "Unknown"

    raise Exception("[ERROR] Miner type '{}' at ip address '{}' is not supported.".format(
        model_name, ip))


def get_miner_instance(miner):
    # if miner not accessible
    miner_stats = get_stats(miner.ip)
    if miner_stats['STATUS'][0]['STATUS'] == 'error':
        return []

    if miner.model.model == "AV741":
        return make_miner_instance_avalon7(miner, miner_stats, get_pools(miner.ip))
    elif miner.model.model == "GekkoScience":
        return make_miner_instance_gekkoscience(miner, miner_stats, get_pools(miner.ip), get_summary(miner.ip))
    elif miner.model.model == "R1-LTC":
        return make_miner_instance_r1_ltc(miner, miner_stats, get_pools(miner.ip), get_summary(miner.ip))
    else:
        return make_miner_instance_bitmain(miner, miner_stats, get_pools(miner.ip))


class miner_instance(object):
    def __init__(self, worker, working_chip_count, defective_chip_count, inactive_chip_count, expected_chip_count, frequency, hashrate_value, hashrate_unit, temps, fan_speeds, fan_pct, hw_error_rate_pct, uptime_secs, verboses, warnings, errors, miner):
        self.worker = worker
        self.working_chip_count = working_chip_count
        self.defective_chip_count = defective_chip_count
        self.inactive_chip_count = inactive_chip_count
        self.expected_chip_count = expected_chip_count
        self.frequency = frequency
        self.hashrate_value = hashrate_value
        self.hashrate_unit = hashrate_unit
        self.temps = temps
        self.fan_speeds = fan_speeds
        self.fan_pct = fan_pct
        self.hw_error_rate_pct = hw_error_rate_pct
        self.uptime = timedelta(seconds=uptime_secs)
        self.miner = miner

        if defective_chip_count > 0:
            errors.append("[WARNING] '{}' chips are defective on miner '{}'.".format(
                defective_chip_count, miner.ip))
        if working_chip_count + defective_chip_count < expected_chip_count:
            error_message = "[ERROR] ASIC chips are missing from miner '{}'. Your Antminer '{}' has '{}/{} chips'." \
                .format(miner.ip,
                        miner.model.model,
                        working_chip_count + defective_chip_count,
                        expected_chip_count)
            errors.append(error_message)
        if temps and max(temps) >= miner.model.high_temp:
            error_message = "[WARNING] High temperatures on miner '{}'.".format(
                miner.ip)
            warnings.append(error_message)
        # Dedupe the messages
        self.warnings = dedupe_messages(warnings)
        self.errors = dedupe_messages(errors)
        self.verboses = dedupe_messages(verboses)

    def fan_speed_pretty(self):
        fan_pct = self.fan_pct
        if fan_pct is None:
            if self.fan_speeds:
                fan_pct = (100.0 * max(self.fan_speeds)) / \
                    self.miner.model.max_fan_rpm
            else:
                fan_pct = 0

        return "{0} / {1:.0f}%".format(str(self.fan_speeds), fan_pct)

    def hashrate_pretty(self):
        return "{:3.2f} {}".format(self.hashrate_value, self.hashrate_unit)

    def frequency_pretty(self):
        frequency_pct = (100.0 * self.frequency) / \
            self.miner.model.default_frequency
        return "{0} / {1:.0f}%".format(self.frequency, frequency_pct)

    def __str__(self):
        return "worker:{} working_chip_count:{} defective_chip_count:{} inactive_chip_count:{} expected_chip_count:{} frequency:{} hashrate_value:{} hashrate_unit:{} temps:{} fan_speeds:{} fan_pct:{} hw_error_rate_pct:{} uptime:{} verboses:{}, warnings:{} errors:{}".format(self.worker, self.working_chip_count, self.inactive_chip_count, self.defective_chip_count, self.expected_chip_count, self.frequency, self.hashrate_value, self.hashrate_unit, self.temps, self.fan_speeds, self.fan_pct, self.hw_error_rate_pct, self.uptime, self.verboses, self.warnings, self.errors)

# Update from one unit to the next if the value is greater than 1024.
# e.g. update_unit_and_value(1024, "GH/s") => (1, "TH/s")


def update_unit_and_value(value, unit):
    while value > 1000:
        value = value / 1000.0
        if unit == 'MH/s':
            unit = 'GH/s'
        elif unit == 'GH/s':
            unit = 'TH/s'
        elif unit == 'TH/s':
            unit = 'PH/s'
        elif unit == 'PH/s':
            unit = 'EH/s'
        else:
            assert False, "Unsupported unit: {}".format(unit)
    return (value, unit)


def make_miner_instance_bitmain(miner, miner_stats, miner_pools):
    # Get worker name
    worker = miner_pools['POOLS'][0]['User']
    # Get miner's ASIC chips
    asic_chains = [miner_stats['STATS'][1][chain] for chain in miner_stats['STATS'][1].keys() if
                   "chain_acs" in chain]
    # count number of working chips
    O = [str(o).count('o') for o in asic_chains]
    Os = sum(O)
    # count number of defective chips
    X = [str(x).count('x') for x in asic_chains]
    Xs = sum(X)
    # get number of in-active chips
    _dash_chips = [str(x).count('-') for x in asic_chains]
    _dash_chips = sum(_dash_chips)
    # Get total number of chips according to miner's model
    # convert miner.model.chips to int list and sum
    chips_list = [int(y) for y in str(miner.model.chips).split(',')]
    total_chips = sum(chips_list)

    # Get the temperatures of the miner according to miner's model
    temps = [int(miner_stats['STATS'][1][temp]) for temp in
             sorted(miner_stats['STATS'][1].keys(), key=lambda x: str(x)) if
             re.search(miner.model.temp_keys + '[0-9]', temp) if miner_stats['STATS'][1][temp] != 0]
    # Get fan speeds
    fan_speeds = [miner_stats['STATS'][1][fan] for fan in
                  sorted(miner_stats['STATS'][1].keys(), key=lambda x: str(x)) if
                  re.search("fan" + '[0-9]', fan) if miner_stats['STATS'][1][fan] != 0]
    # Frequency
    frequency = float(str(miner_stats['STATS'][1]['frequency']))
    # Get GH/S 5s
    hashrate_value = float(str(miner_stats['STATS'][1]['GHS 5s']))
    hashrate_unit = miner.model.hashrate_unit
    hashrate_value, hashrate_unit = update_unit_and_value(
        hashrate_value, hashrate_unit)

    # Get HW Errors
    hw_error_rate = miner_stats['STATS'][1]['Device Hardware%']
    # Get uptime
    uptime = miner_stats['STATS'][1]['Elapsed']

    return [miner_instance(worker=worker,
                           working_chip_count=Os,
                           defective_chip_count=Xs,
                           inactive_chip_count=_dash_chips,
                           expected_chip_count=total_chips,
                           frequency=frequency,
                           hashrate_value=hashrate_value,
                           hashrate_unit=hashrate_unit,
                           temps=temps,
                           fan_speeds=fan_speeds,
                           fan_pct=None,
                           hw_error_rate_pct=hw_error_rate,
                           uptime_secs=uptime,
                           verboses=[],
                           warnings=[],
                           errors=[],
                           miner=miner)]


def make_miner_instance_avalon7(miner, miner_stats, miner_pools):
    # Get worker name
    worker = miner_pools['POOLS'][0]['User']

    expected_asic = int(miner.model.chips)

    result = []
    r = re.compile("MM ID(\d*)")
    for i in miner_stats["STATS"]:
        for j in i.keys():
            # Is it a device
            for k in r.findall(j):
                identifier = k[0]
                elapsed = 0
                temps = []
                fan = 0
                fan_pct = 0.0
                frequency = 0
                hashrate_value = 0
                asic_count = 0
                hw_error_rate_pct = 0

                # TODO: Read inactive chip count
                for l in re.findall(r'([A-Za-z]*)\[([0-9]*\.?[0-9]+)(?:%?)\]', i[j]):
                    if l[0] == 'Elapsed':
                        elapsed = int(l[1])
                    elif l[0] == 'Fan':
                        fan = int(l[1])
                    elif l[0] == 'FanR':
                        fan_pct = float(l[1])
                    elif l[0] == 'Freq':
                        frequency = float(l[1])
                    elif l[0] == 'DH':
                        hw_error_rate_pct = float(l[1])
                    elif l[0] == 'GHSmm':
                        hashrate_value = float(l[1])

                hashrate_value, hashrate_unit = update_unit_and_value(
                    hashrate_value, miner.model.hashrate_unit)

                # Read asic info. The following two fields will contain
                # information about how the board is operating and the
                # chipcount.
                for l in re.findall(r'(:?\w*)\[(\d*(?:\s\d*)+)\]', i[j]):
                    if l[0] == 'ECHU':
                        (verboses, warnings, errors) = decode_echu(
                            miner, hashrate_value, identifier, l[1])
                    else:
                        if re.match(r'MW\d+', l[0]):
                            asic_count += decode_mw(l[1])

                for l in re.findall(r'PVT_T\[((?:\s*\d*-\d*\/\d*-\d*\/\d*)*)\]', i[j]):
                    temps = pvt_t_decode(l)

                result.append(miner_instance(worker=worker,
                                             working_chip_count=asic_count,
                                             defective_chip_count=expected_asic - asic_count,
                                             inactive_chip_count=0,
                                             expected_chip_count=expected_asic,
                                             frequency=frequency,
                                             hashrate_value=hashrate_value,
                                             hashrate_unit=hashrate_unit,
                                             temps=temps,
                                             fan_speeds=[fan],
                                             fan_pct=fan_pct,
                                             hw_error_rate_pct=hw_error_rate_pct,
                                             uptime_secs=elapsed,
                                             verboses=verboses,
                                             warnings=warnings,
                                             errors=errors,
                                             miner=miner))
    return result


class AvalonErrorType(Enum):
    IGNORABLE = 1
    WARNING = 2
    FATAL = 3


class AvalonErrorCode(Enum):
    CODE_IDLE = 1
    CODE_MMCRCFAILED = 2
    CODE_NOFAN = 4
    CODE_LOCK = 8
    CODE_APIFIFOOVERFLOW = 16
    CODE_RBOVERFLOW = 32
    CODE_TOOHOT = 64
    CODE_HOTBEFORE = 128
    CODE_LOOPFAILED = 256
    CODE_CORETESTFAILED = 512
    CODE_INVALIDPMU = 1024
    CODE_PGFAILED = 2048
    CODE_NTC_ERR = 4096
    CODE_VOL_ERR = 8192
    CODE_VCORE_ERR = 16384
    CODE_PMUCRCFAILED = 32768
    CODE_INVALID_PLL_VALUE = 65536
    CODE_HUFAILED = 131072

    def get_error_type(self):
        return {
            AvalonErrorCode.CODE_IDLE: AvalonErrorType.WARNING,
            AvalonErrorCode.CODE_MMCRCFAILED: AvalonErrorType.IGNORABLE,
            AvalonErrorCode.CODE_NOFAN: AvalonErrorType.FATAL,
            AvalonErrorCode.CODE_LOCK: AvalonErrorType.WARNING,
            AvalonErrorCode.CODE_APIFIFOOVERFLOW: AvalonErrorType.IGNORABLE,
            AvalonErrorCode.CODE_RBOVERFLOW: AvalonErrorType.IGNORABLE,
            AvalonErrorCode.CODE_TOOHOT: AvalonErrorType.FATAL,
            AvalonErrorCode.CODE_HOTBEFORE: AvalonErrorType.WARNING,
            AvalonErrorCode.CODE_LOOPFAILED: AvalonErrorType.IGNORABLE,
            AvalonErrorCode.CODE_CORETESTFAILED: AvalonErrorType.IGNORABLE,
            AvalonErrorCode.CODE_INVALIDPMU: AvalonErrorType.FATAL,
            AvalonErrorCode.CODE_PGFAILED: AvalonErrorType.FATAL,
            AvalonErrorCode.CODE_NTC_ERR: AvalonErrorType.FATAL,
            AvalonErrorCode.CODE_VOL_ERR: AvalonErrorType.FATAL,
            AvalonErrorCode.CODE_VCORE_ERR: AvalonErrorType.FATAL,
            AvalonErrorCode.CODE_PMUCRCFAILED: AvalonErrorType.IGNORABLE,
            AvalonErrorCode.CODE_INVALID_PLL_VALUE: AvalonErrorType.WARNING,
            AvalonErrorCode.CODE_HUFAILED: AvalonErrorType.WARNING
        }[AvalonErrorCode(self.value)]

    def get_logging_type(self):
        return {
            AvalonErrorType.IGNORABLE: "VERBOSE",
            AvalonErrorType.WARNING: "WARNING",
            AvalonErrorType.FATAL: "ERROR"
        }[self.get_error_type()]

    def get_error_message(self, ip, identifier):
        return "[{}] {}/{} - {}: {}".format(self.get_logging_type(), ip, identifier, self.name, {
            AvalonErrorCode.CODE_IDLE: "Check if the network is ok or AUC work normally",
            AvalonErrorCode.CODE_MMCRCFAILED: "Ignore it please, If CGMiner is restart, It will be ok.",
            AvalonErrorCode.CODE_NOFAN: "Fan cann't be found. Check the fan connection",
            AvalonErrorCode.CODE_LOCK: "MM is not permit to run without decrypt. Ask for unlock service",
            AvalonErrorCode.CODE_APIFIFOOVERFLOW: "It's just a notice on api fifo",
            AvalonErrorCode.CODE_RBOVERFLOW: "Ignore it please, If CGMiner is restart, It will be ok.",
            AvalonErrorCode.CODE_TOOHOT: "Check the fan or replace the too hot module",
            AvalonErrorCode.CODE_HOTBEFORE: "It's just a notice for too hot modular",
            AvalonErrorCode.CODE_LOOPFAILED: "The led will turn to red. Ignore it if hashrate is not so bad.",
            AvalonErrorCode.CODE_CORETESTFAILED: "Find bad chip on channel 0 or channel 1. Ignore it if hashrate is not so bad",
            AvalonErrorCode.CODE_INVALIDPMU: "Replace a new pmu",
            AvalonErrorCode.CODE_PGFAILED: "Check the PMU",
            AvalonErrorCode.CODE_NTC_ERR: "Replace the module which the code was found on.",
            AvalonErrorCode.CODE_VOL_ERR: "Replace the module which the code was found on.",
            AvalonErrorCode.CODE_VCORE_ERR: "Replace the module which the code was found on.",
            AvalonErrorCode.CODE_PMUCRCFAILED: "Ignore it please",
            AvalonErrorCode.CODE_INVALID_PLL_VALUE: "Reboot the modular, if not good then try to replace the module.",
            AvalonErrorCode.CODE_HUFAILED: "Reboot the modular, if not good then try to replace the HU."
        }[AvalonErrorCode(self.value)])

# Example:
# 784 0 0 0


def decode_echu(miner, current_hashrate, identifier, input):
    if type(current_hashrate) is not float:
        return ["INTERNAL_ERROR"]

    # Target hashrate will be 80% of the model advertised hashrate
    # This is because some error codes are ignorable if the hashrate
    # is not too bad. They don't define what too bad means, so we arbitrarily
    # define as 80%.
    target_hashrate = int(miner.model.hashrate_value * 0.80)

    infos = []
    warnings = []
    errors = []
    actual_codes = input.split(" ")
    # Lets decode each given code.
    for actual_code in actual_codes:
        actual_code = int(actual_code)
        # For all available codes.
        for code in list(AvalonErrorCode):
            if (code.value & actual_code) <> 0:
                # Acording to doc there are some erros that are ignorable.
                if code.get_error_type() == AvalonErrorType.IGNORABLE:
                    if current_hashrate >= target_hashrate:
                        infos.append(code.get_error_message(
                            miner.ip, identifier))
                    else:
                        warnings.append(
                            code.get_error_message(miner.ip, identifier))
                elif code.get_error_type() == AvalonErrorType.WARNING:
                    warnings.append(
                        code.get_error_message(miner.ip, identifier))
                else:
                    errors.append(code.get_error_message(miner.ip, identifier))
    return (infos, warnings, errors)

# MW will be something like:
# 408 407 452 402 407 387 425 427 392 449 415 442 447 425 405 392 450 417 386 387 426 448
# We just need to count the number of numbers.


def decode_mw(input):
    nums = input.split(" ")
    return len(nums)


def pvt_t_decode(input):
    temps = []
    for temp_block_str in input.split(" "):
        for temp_tuple_str in re.findall(r'(\d*)-(\d*)\/(\d*)-(\d*)\/(\d*)', temp_block_str):
            for temp_str in temp_tuple_str:
                temps.append(int(temp_str))
    temps.sort(reverse=True)
    # Just get the top 4 temps.
    return temps[0:min(4, len(temps))]


def make_miner_instance_gekkoscience(miner, miner_stats, miner_pools, miner_summary):
    # Get worker name
    worker = miner_pools['POOLS'][0]['User']

    # Get GH/S 5s
    hashrate_value = float(str(miner_summary['SUMMARY'][0]['MHS 5s']))
    hashrate_unit = miner.model.hashrate_unit
    hashrate_value, hashrate_unit = update_unit_and_value(
        hashrate_value, hashrate_unit)

    # Get HW Errors
    hw_error_rate = miner_summary['SUMMARY'][0]['Device Hardware%']
    # Get uptime
    uptime = miner_summary['SUMMARY'][0]['Elapsed']

    return [miner_instance(worker=worker,
                           working_chip_count=int(miner.model.chips),
                           defective_chip_count=0,
                           inactive_chip_count=0,
                           expected_chip_count=int(miner.model.chips),
                           frequency=200,
                           hashrate_value=hashrate_value,
                           hashrate_unit=hashrate_unit,
                           temps=[],
                           fan_speeds=[],
                           fan_pct=None,
                           hw_error_rate_pct=hw_error_rate,
                           uptime_secs=uptime,
                           verboses=[],
                           warnings=[],
                           errors=[],
                           miner=miner)]


def make_miner_instance_r1_ltc(miner, miner_stats, miner_pools, miner_summary):
    # Get worker name
    worker = miner_pools['POOLS'][0]['User']

    # Get GH/S 5s
    hashrate_value = float(str(miner_summary['SUMMARY'][0]['GHS 5s']))
    hashrate_unit = miner.model.hashrate_unit
    hashrate_value, hashrate_unit = update_unit_and_value(
        hashrate_value, hashrate_unit)

    # Get HW Errors
    hw_error_rate = miner_summary['SUMMARY'][0]['Device Hardware%']
    # Get uptime
    uptime = miner_summary['SUMMARY'][0]['Elapsed']

    return [miner_instance(worker=worker,
                           working_chip_count=int(miner.model.chips),
                           defective_chip_count=0,
                           inactive_chip_count=0,
                           expected_chip_count=int(miner.model.chips),
                           frequency=100,
                           hashrate_value=hashrate_value,
                           hashrate_unit=hashrate_unit,
                           temps=[],
                           fan_speeds=[],
                           fan_pct=None,
                           hw_error_rate_pct=hw_error_rate,
                           uptime_secs=uptime,
                           verboses=[],
                           warnings=[],
                           errors=[],
                           miner=miner)]
