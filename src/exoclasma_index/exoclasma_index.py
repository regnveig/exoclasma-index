__scriptname__ = 'exoclasma-index'
__version__ = '0.9.0'
__bugtracker__ = 'https://github.com/regnveig/exoclasma-index/issues'

from Bio import SeqIO #
import argparse
import bz2
import gzip
import json
import logging
import os
import pandas #
import re
import subprocess
import sys
import tempfile

# -----=====| LOGGING |=====-----

logging.basicConfig(format='[%(levelname)s] %(message)s', level=logging.INFO)

def CheckDependency(Name):
	Shell = subprocess.Popen(Name, shell = True, executable = 'bash', stdout = subprocess.PIPE, stderr = subprocess.PIPE)
	Stdout, _ = Shell.communicate()
	if Shell.returncode == 127:
		logging.error(f'Dependency "{Name}" is not found!')
		exit(1)
	if Shell.returncode == 126:
		logging.error(f'Dependency "{Name}" is not executable!')
		exit(1)

def CheckDependencies():
	CheckDependency('samtools')
	CheckDependency('bwa')
	CheckDependency('bedtools')
	CheckDependency('gatk')


## ------======| MISC |======------

def Open(FileName):
	GzipCheck = lambda FileName: open(FileName, 'rb').read(2).hex() == '1f8b'
	Bzip2Check = lambda FileName: open(FileName, 'rb').read(3).hex() == '425a68'
	CheckFlags = GzipCheck(FileName = FileName), Bzip2Check(FileName = FileName)
	OpenFunc = { (0, 1): bz2.open, (1, 0): gzip.open, (0, 0): open }[CheckFlags]
	return OpenFunc(FileName, 'rt')

def ArmorDoubleQuotes(String): return f'"{String}"'

def ArmorSingleQuotes(String): return f"'{String}'"

def BashSubprocess(SuccessMessage, Command):
	logging.debug(f'Shell command: {Command}')
	Shell = subprocess.Popen(Command, shell = True, executable = 'bash', stdout = subprocess.PIPE, stderr = subprocess.PIPE)
	_, Stderr = Shell.communicate()
	if (Shell.returncode != 0):
		logging.error(f'Shell command returned non-zero exit code {Shell.returncode}: {Command}\n{Stderr.decode("utf-8")}')
		exit(1)
	logging.info(SuccessMessage)

## ------======| REFSEQ PREP |======------

def CreateGenomeInfo(GenomeName, RestrictionEnzymes):
	ConfigJson = {
		'name':               str(GenomeName),
		'fasta':              f'{GenomeName}.fa',
		'chrom.sizes':        f'{GenomeName}.chrom.sizes',
		'samtools.faidx':     f'{GenomeName}.fa.fai',
		'bed':                f'{GenomeName}.bed',
		'gatk.dict':          f'{GenomeName}.dict',
		'juicer.rs':          { Name: os.path.join('juicer.rs', f'{GenomeName}.rs.{Name}.txt') for Name in RestrictionEnzymes.keys() },
		'capture':            dict()
	}
	return ConfigJson

def RefseqPreparation(GenomeName, FastaPath, ParentDir):
	logging.info(f'{__scriptname__} Reference {__version__}')
	# Config
	ConfigPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
	Config = json.load(open(ConfigPath, 'rt'))
	# Output Dir
	OutputDir = os.path.realpath(os.path.join(ParentDir, GenomeName))
	RSDir = os.path.realpath(os.path.join(OutputDir, 'juicer.rs'))
	os.mkdir(OutputDir)
	os.mkdir(RSDir)
	logging.info(f'Directory created: {OutputDir}')
	# Open fasta
	FullFastaPath = os.path.realpath(FastaPath)
	Fasta = SeqIO.parse(Open(FullFastaPath), 'fasta')
	logging.info(f'FASTA opened: {FullFastaPath}')
	SearchQueries = { Name: re.compile(Sequences) for Name, Sequences in Config['Enzymes'].items() }
	GenomeInfo = CreateGenomeInfo(GenomeName, Config['Enzymes'])
	# Paths
	OutputFasta = os.path.join(OutputDir, GenomeInfo['fasta'])
	ChromSizesPath = os.path.join(OutputDir, GenomeInfo['chrom.sizes'])
	BedPath = os.path.join(OutputDir, GenomeInfo['bed'])
	with open(OutputFasta, 'w') as NewFasta, open(ChromSizesPath, 'w') as ChromSizes, open(BedPath, 'w') as BedFile:
		for Contig in Fasta:
			Contig.name = re.sub('[^\w\.]', '_', Contig.name)
			Seq = Contig.seq.__str__()
			SeqLength = len(Seq)
			SeqIO.write([Contig], NewFasta, 'fasta')
			ChromSizes.write(f'{Contig.name}\t{SeqLength}\n')
			BedFile.write(f'{Contig.name}\t0\t{SeqLength}\n')
			for Enzyme, Query in SearchQueries.items():
				RSPath = os.path.join(OutputDir, GenomeInfo['juicer.rs'][Enzyme])
				with open(RSPath, 'a') as FileWrapper:
					FileWrapper.write(' '.join([Contig.name] + [str(Match.start() + 1) for Match in Query.finditer(Seq)] + [str(SeqLength)]) + '\n')
			logging.info(f'Contig ready: {Contig.name}')
	logging.info('Fasta, chrom sizes, bed file, and restriction sites are ready')
	CommandSamtoolsIndex = ['samtools', 'faidx',  ArmorDoubleQuotes(OutputFasta)]
	CommandBwaIndex = ['bwa', 'index',  ArmorDoubleQuotes(OutputFasta)]
	CommandGATKIndex = ['gatk', 'CreateSequenceDictionary', '--VERBOSITY', 'ERROR', '-R',  ArmorDoubleQuotes(OutputFasta)]
	BashSubprocess('SAMtools faidx ready', ' '.join(CommandSamtoolsIndex))
	BashSubprocess('BWA index ready', ' '.join(CommandBwaIndex))
	BashSubprocess('GATK dictionary ready', ' '.join(CommandGATKIndex))
	GenomeInfoJson = os.path.join(OutputDir, f'{GenomeName}.info.json')
	json.dump(GenomeInfo, open(GenomeInfoJson, 'wt'), indent = 4, ensure_ascii = False)
	logging.info('Job finished')


## ------======| CAPTURE PREP |======------

def CreateCaptureInfo(CaptureName):
	ConfigJson = {
		'name':         str(CaptureName),
		'capture':      os.path.join('capture', CaptureName, f'{CaptureName}.capture.bed'),
		'not.capture':  os.path.join('capture', CaptureName, f'{CaptureName}.not.capture.bed')
	}
	return ConfigJson

def CapturePreparation(CaptureName, InputBED, GenomeInfoJSON):
	logging.info(f'{__scriptname__} Capture {__version__}')
	# Info struct
	GenomeInfoPath = os.path.realpath(GenomeInfoJSON)
	GenomeInfo = json.load(open(GenomeInfoPath, 'rt'))
	logging.info(f'Genome info loaded: {GenomeInfoPath}')
	CaptureInfo = CreateCaptureInfo(CaptureName)
	BedAdjustFunction = r"sed -e 's/$/\t\./'"
	# Paths
	InputPath = os.path.realpath(InputBED)
	GenomeDir = os.path.dirname(GenomeInfoPath)
	CaptureDir = os.path.join(GenomeDir, 'capture')
	OutputDir = os.path.join(CaptureDir, CaptureName)
	GenomeInfoBed = os.path.join(GenomeDir, GenomeInfo['bed'])
	CapturePath = os.path.join(GenomeDir, CaptureInfo['capture'])
	NotCapturePath = os.path.join(GenomeDir, CaptureInfo['not.capture'])
	TempPurified = os.path.join(CaptureDir, '.temp.decompressed.bed')
	ChromSizes = pandas.read_csv(GenomeInfoBed, sep = '\t', header = None).set_index(0)[2].to_dict()
	# Make dirs
	try:
		os.mkdir(CaptureDir)
	except FileExistsError:
		logging.info('Captures directory already exists. Passing.')
	os.mkdir(OutputDir)
	logging.info('Directories created')
	with Open(InputPath) as Input, open(TempPurified, 'wt') as Purified:
		for Line in Input:
			SplitLine = Line.split('\t')
			ParsedLine = { 'Contig': re.sub('[^\w\.]', '_', SplitLine[0]), 'Start': int(SplitLine[1]), 'End': int(SplitLine[2]) }
			assert ParsedLine['Contig'] in ChromSizes, f'Contig "{ParsedLine["Contig"]}" is not presented in the reference: "{Line}"'
			assert ParsedLine['End'] > ParsedLine['Start'], f'Zero length interval in the BED file: "{Line}"'
			assert ParsedLine['End'] < ChromSizes[ParsedLine['Contig']], f'Interval goes out of the contig: "{Line}"'
			Purified.write(f'{ParsedLine["Contig"]}\t{ParsedLine["Start"]}\t{ParsedLine["End"]}\n')
	logging.info('BED file decompressed and purified')
	CommandFilterAndSort = ['set', '-o', 'pipefail;', 'bedtools', 'sort', '-faidx', ArmorDoubleQuotes(GenomeInfoBed), '-i', ArmorDoubleQuotes(TempPurified), '|', BedAdjustFunction, '>', ArmorDoubleQuotes(CapturePath)]
	CommandNotCapture = ['bedtools', 'subtract', '-a', ArmorDoubleQuotes(GenomeInfoBed), '-b', ArmorDoubleQuotes(CapturePath), '|', BedAdjustFunction, '>', ArmorDoubleQuotes(NotCapturePath)]
	BashSubprocess('Capture sorted and written', ' '.join(CommandFilterAndSort))
	BashSubprocess('NotCapture written', ' '.join(CommandNotCapture))
	GenomeInfo['capture'][CaptureName] = CaptureInfo
	json.dump(GenomeInfo, open(GenomeInfoPath, 'wt'), indent = 4, ensure_ascii = False)
	logging.info('Genome Info updated')
	os.remove(TempPurified)
	logging.info('Job finished')

def CreateParser():
	Parser = argparse.ArgumentParser(
		formatter_class = argparse.RawDescriptionHelpFormatter,
		description = f'{__scriptname__} {__version__}: Reference Sequence and Capture Intervals Preparation for ExoClasma Suite',
		epilog = f'Bug tracker: {__bugtracker__}')
	Parser.add_argument('-v', '--version', action = 'version', version = __version__)
	Subparsers = Parser.add_subparsers(title = 'Commands', dest = 'command')
	# PrepareReference
	PrepareReferenceParser = Subparsers.add_parser('Reference', help = f'Prepare Reference Sequence. Create genomic indices for several tools, restriction sites, and GenomeInfo JSON file.')
	PrepareReferenceParser.add_argument('-f', '--fasta', required = True, type = str, help = f'Raw FASTA file. May be gzipped or bzipped')
	PrepareReferenceParser.add_argument('-n', '--name', required = True, type = str, help = f'Name of reference assembly. Will be used as folder name and files prefix')
	PrepareReferenceParser.add_argument('-p', '--parent', required = True, type = str, help = f'Parent dir where reference folder will be created')
	# PrepareCapture
	PrepareCaptureParser = Subparsers.add_parser('Capture', help = f'Prepare Capture BED. Filter and sort Capture BED, create NotCapture and update GenomeInfo JSON files')
	PrepareCaptureParser.add_argument('-b', '--bed', required = True, type = str, help = f'Raw BED file')
	PrepareCaptureParser.add_argument('-n', '--name', required = True, type = str, help = f'Name of capture. Will be used as folder name and files prefix')
	PrepareCaptureParser.add_argument('-g', '--genomeinfo', required = True, type = str, help = f'GenomeInfo JSON file. See "exoclasma-index Reference --help" for details')
	return Parser

def main():
	CheckDependencies()
	Parser = CreateParser()
	Namespace = Parser.parse_args(sys.argv[1:])
	if Namespace.command == "Reference":
		FastaPath, GenomeName, ParentDir = os.path.abspath(Namespace.fasta), Namespace.name, os.path.abspath(Namespace.parent)
		RefseqPreparation(FastaPath = FastaPath, GenomeName = GenomeName, ParentDir = ParentDir)
	elif Namespace.command == "Capture":
		CaptureName, InputBED, GenomeInfoJSON = Namespace.name, os.path.abspath(Namespace.bed), os.path.abspath(Namespace.genomeinfo)
		CapturePreparation(CaptureName = CaptureName, InputBED = InputBED, GenomeInfoJSON = GenomeInfoJSON)
	else: Parser.print_help()

if __name__ == '__main__': main()